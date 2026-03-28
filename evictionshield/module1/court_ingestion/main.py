"""
EvictionShield — Module 1A: Court Filing Ingestion Service
Cloud Run service that polls court APIs on a configurable schedule and
publishes each new filing to Pub/Sub topic `raw-eviction-filings`.

Environment variables (non-sensitive):
    GCP_PROJECT_ID          Google Cloud project ID
    GCS_BUCKET_RAW          Cloud Storage bucket for raw filings
    PUBSUB_TOPIC_RAW        Pub/Sub topic name (default: raw-eviction-filings)
    POLL_INTERVAL_SECONDS   Polling interval (default: 14400 = 4 hours)
    STATE_ADAPTERS          Comma-separated list of enabled adapters (default: PA)
    LOG_LEVEL               Logging level (default: INFO)

Credentials are read from Secret Manager at runtime — never from environment variables.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Type

from google.cloud import firestore, pubsub_v1, secretmanager
from google.cloud.pubsub_v1.types import PubsubMessage

from adapters.base import AuthenticationError, CourtAPIAdapter, FetchError, NormalizedFiling
from adapters.pennsylvania_ujs import PennsylvaniaUJSAdapter

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(message)s",  # Structured JSON via Cloud Logging agent
)
logger = logging.getLogger(__name__)


def _log(severity: str, message: str, **kwargs: Any) -> None:
    """Emit a Cloud Logging-compatible structured JSON log line."""
    record: Dict[str, Any] = {
        "severity": severity,
        "message": message,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "service": "court-ingestion",
        **kwargs,
    }
    print(json.dumps(record), flush=True)


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

_ADAPTER_REGISTRY: Dict[str, Type[CourtAPIAdapter]] = {
    "PA": PennsylvaniaUJSAdapter,
    # Add new states here:  "CA": CaliforniaCourtAdapter, etc.
}


def _build_adapter(state: str, project_id: str, bucket: str) -> CourtAPIAdapter:
    """Instantiate the correct adapter for a given state code."""
    adapter_cls = _ADAPTER_REGISTRY.get(state.upper())
    if adapter_cls is None:
        raise ValueError(f"No adapter registered for state '{state}'. "
                         f"Available: {list(_ADAPTER_REGISTRY)}")
    if state.upper() == "PA":
        return PennsylvaniaUJSAdapter(
            gcp_project_id=project_id,
            gcs_bucket_raw=bucket,
        )
    # Generic construction for future adapters that share the same signature
    return adapter_cls(gcp_project_id=project_id, gcs_bucket_raw=bucket)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------

def _retry_with_backoff(
    fn,
    *args,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retryable_exceptions=(FetchError, AuthenticationError, Exception),
    **kwargs,
):
    """
    Call fn(*args, **kwargs) with exponential backoff retry.

    Raises the last exception if all attempts are exhausted.
    AuthenticationError is retried (credentials may have been rotated).
    """
    delay = base_delay
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            _log(
                "WARNING",
                f"Attempt {attempt}/{max_attempts} failed, retrying in {delay:.1f}s",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            time.sleep(delay)
            delay = min(delay * 2, max_delay)

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Pub/Sub publisher
# ---------------------------------------------------------------------------

class FilingPublisher:
    """Wraps the Pub/Sub publisher with structured logging and error handling."""

    def __init__(self, project_id: str, topic_name: str):
        self._project_id = project_id
        self._topic_path = f"projects/{project_id}/topics/{topic_name}"
        self._publisher = pubsub_v1.PublisherClient()

    def publish(self, filing: NormalizedFiling) -> str:
        """
        Serialise the filing, publish to Pub/Sub, return the message ID.
        The message is idempotent: re-publishing the same filing (same case_number)
        is safe because downstream consumers deduplicate on case_number.
        """
        payload = json.dumps(filing.to_pubsub_dict()).encode("utf-8")
        future = self._publisher.publish(
            self._topic_path,
            data=payload,
            case_number=filing.case_number,
            state=filing.state,
            jurisdiction=filing.jurisdiction_code,
        )
        message_id = future.result(timeout=30)
        _log(
            "INFO",
            "Filing published to Pub/Sub",
            case_number=filing.case_number,
            message_id=message_id,
            topic=self._topic_path,
        )
        return message_id


# ---------------------------------------------------------------------------
# Checkpoint management (Firestore)
# ---------------------------------------------------------------------------

class IngestionCheckpoint:
    """
    Stores the last-successful poll timestamp per adapter in Firestore.
    Document path: ingestion_checkpoints/{adapter_id}
    """

    _COLLECTION = "ingestion_checkpoints"

    def __init__(self, project_id: str):
        self._db = firestore.Client(project=project_id)

    def get_last_polled(self, adapter_id: str, default_lookback_hours: int = 4) -> datetime:
        doc = self._db.collection(self._COLLECTION).document(adapter_id).get()
        if doc.exists:
            ts = doc.to_dict().get("last_polled_at")
            if ts:
                # Firestore returns a datetime; make tz-naive for comparison consistency
                return ts.replace(tzinfo=None) if ts.tzinfo else ts
        # First run — look back default_lookback_hours
        return datetime.utcnow() - timedelta(hours=default_lookback_hours)

    def set_last_polled(self, adapter_id: str, ts: datetime) -> None:
        self._db.collection(self._COLLECTION).document(adapter_id).set(
            {"last_polled_at": ts, "adapter_id": adapter_id}, merge=True
        )


# ---------------------------------------------------------------------------
# Ingestion loop
# ---------------------------------------------------------------------------

class IngestionService:
    """
    Main service class. Runs the poll → download → publish loop for each adapter.
    """

    def __init__(
        self,
        project_id: str,
        gcs_bucket_raw: str,
        pubsub_topic: str,
        state_codes: list[str],
        poll_interval_seconds: int,
    ):
        self._project_id = project_id
        self._gcs_bucket_raw = gcs_bucket_raw
        self._publisher = FilingPublisher(project_id, pubsub_topic)
        self._checkpoint = IngestionCheckpoint(project_id)
        self._poll_interval = poll_interval_seconds
        self._adapters: Dict[str, CourtAPIAdapter] = {
            s: _build_adapter(s, project_id, gcs_bucket_raw) for s in state_codes
        }
        self._running = True

    def _poll_adapter(self, state: str, adapter: CourtAPIAdapter) -> int:
        """
        Run one poll cycle for a single adapter.
        Returns the number of filings published.
        """
        adapter_id = adapter.get_adapter_id()
        since = self._checkpoint.get_last_polled(adapter_id)
        _log("INFO", "Starting poll cycle", adapter=adapter_id, since=since.isoformat())

        # 1. Fetch new filings (with retry)
        raw_filings = _retry_with_backoff(
            adapter.fetch_new_filings, since,
            max_attempts=5,
            retryable_exceptions=(FetchError, Exception),
        )
        _log("INFO", f"Fetched {len(raw_filings)} raw filings", adapter=adapter_id)

        published = 0
        errors = 0

        for raw in raw_filings:
            try:
                # 2. Parse
                parsed = adapter.parse_filing_response(raw)

                # 3. Download document
                if isinstance(adapter, PennsylvaniaUJSAdapter) and parsed.get("raw_download_url"):
                    doc_bytes = _retry_with_backoff(
                        adapter.download_document,
                        parsed["raw_download_url"],
                        max_attempts=3,
                    )
                    checksum = CourtAPIAdapter.compute_checksum(doc_bytes)
                    doc_uri = adapter.upload_to_gcs(
                        doc_bytes, parsed["case_number"], parsed["document_format"]
                    )
                else:
                    doc_bytes = b""
                    checksum = None
                    doc_uri = parsed.get("raw_download_url", "")

                # 4. Normalize
                filing: NormalizedFiling = adapter.normalize_to_schema(parsed, doc_uri)
                filing.checksum_sha256 = checksum

                # 5. Publish
                self._publisher.publish(filing)
                published += 1

            except Exception as exc:
                errors += 1
                _log(
                    "ERROR",
                    "Failed to process filing",
                    adapter=adapter_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    raw_preview=str(raw)[:200],
                )

        # 6. Advance checkpoint only if at least some succeeded
        if published > 0 or len(raw_filings) == 0:
            self._checkpoint.set_last_polled(adapter_id, datetime.utcnow())

        _log(
            "INFO",
            "Poll cycle complete",
            adapter=adapter_id,
            published=published,
            errors=errors,
        )
        return published

    def run_once(self) -> Dict[str, int]:
        """Run a single poll cycle across all configured adapters."""
        results: Dict[str, int] = {}
        for state, adapter in self._adapters.items():
            try:
                results[state] = self._poll_adapter(state, adapter)
            except Exception as exc:
                _log(
                    "CRITICAL",
                    "Adapter poll cycle failed completely",
                    state=state,
                    error=str(exc),
                )
                results[state] = 0
        return results

    def run_forever(self) -> None:
        """
        Main loop: poll all adapters every self._poll_interval seconds.
        Responds to SIGTERM for graceful Cloud Run shutdown.
        """
        def _handle_sigterm(signum, frame):
            _log("INFO", "SIGTERM received — shutting down gracefully")
            self._running = False

        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigterm)

        _log("INFO", "EvictionShield ingestion service started", adapters=list(self._adapters))

        while self._running:
            start = time.monotonic()
            self.run_once()
            elapsed = time.monotonic() - start
            sleep_time = max(0, self._poll_interval - elapsed)
            _log("INFO", f"Sleeping {sleep_time:.0f}s until next poll cycle")
            # Sleep in 1-second increments to allow fast SIGTERM handling
            for _ in range(int(sleep_time)):
                if not self._running:
                    break
                time.sleep(1)

        _log("INFO", "EvictionShield ingestion service stopped")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    project_id = os.environ["GCP_PROJECT_ID"]
    gcs_bucket_raw = os.environ["GCS_BUCKET_RAW"]
    pubsub_topic = os.environ.get("PUBSUB_TOPIC_RAW", "raw-eviction-filings")
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "14400"))
    state_codes = [s.strip() for s in os.environ.get("STATE_ADAPTERS", "PA").split(",")]

    service = IngestionService(
        project_id=project_id,
        gcs_bucket_raw=gcs_bucket_raw,
        pubsub_topic=pubsub_topic,
        state_codes=state_codes,
        poll_interval_seconds=poll_interval,
    )
    service.run_forever()


if __name__ == "__main__":
    main()
