"""
EvictionShield — Module 1A: Pennsylvania UJS (Unified Judicial System) Adapter

Reference implementation of CourtAPIAdapter for Pennsylvania's public portal.
PA UJS exposes a REST/OData-style endpoint for magisterial district court filings.
Authentication uses OAuth2 client-credentials flow.

Docs reference: https://ujsportal.pacourts.us (public portal; bulk API requires registration)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from google.cloud import secretmanager, storage

from .base import (
    AuthenticationError,
    CourtAPIAdapter,
    DocumentFormat,
    FetchError,
    NormalizedFiling,
    ParseError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PA_TOKEN_URL = "https://ujsportal.pacourts.us/oauth/token"
_PA_FILINGS_URL = "https://ujsportal.pacourts.us/api/v1/cases"
_PA_DOCUMENT_URL = "https://ujsportal.pacourts.us/api/v1/documents/{doc_id}"

_PA_FILING_TYPES = frozenset(["LANDLORD-TENANT", "LT-EVICTION", "MD-LT"])

_NOTICE_TYPE_MAP = {
    "NON-PAYMENT": "pay_or_quit",
    "BREACH": "cure_or_quit",
    "TERMINATION": "unconditional_quit",
    "HOLDOVER": "holdover",
}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class PennsylvaniaUJSAdapter(CourtAPIAdapter):
    """
    Adapter for Pennsylvania Unified Judicial System bulk filing API.

    Credentials secret name in Secret Manager:
        court-api-creds-pa

    Expected secret payload (JSON):
        {
            "client_id":     "<OAuth2 client ID>",
            "client_secret": "<OAuth2 client secret>",
            "scope":         "bulk-filings"
        }
    """

    def __init__(
        self,
        gcp_project_id: str,
        gcs_bucket_raw: str,
        jurisdiction_code: str = "PA-UJS",
        page_size: int = 200,
        secret_name: str = "court-api-creds-pa",
    ):
        super().__init__(state_code="PA", jurisdiction_code=jurisdiction_code)
        self._gcp_project_id = gcp_project_id
        self._gcs_bucket_raw = gcs_bucket_raw
        self._page_size = page_size
        self._secret_name = secret_name
        self._secret_client = secretmanager.SecretManagerServiceClient()
        self._storage_client = storage.Client(project=gcp_project_id)

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """
        Retrieve OAuth2 client credentials from Secret Manager and obtain
        a bearer token from the PA UJS token endpoint.
        """
        secret_path = (
            f"projects/{self._gcp_project_id}/secrets/{self._secret_name}/versions/latest"
        )
        try:
            response = self._secret_client.access_secret_version(name=secret_path)
            creds = json.loads(response.payload.data.decode("utf-8"))
        except Exception as exc:
            raise AuthenticationError(
                f"Failed to load PA UJS credentials from Secret Manager: {exc}"
            ) from exc

        payload = {
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "scope": creds.get("scope", "bulk-filings"),
        }
        try:
            resp = requests.post(
                _PA_TOKEN_URL,
                data=payload,
                timeout=15,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise AuthenticationError(
                f"PA UJS OAuth token request failed [{resp.status_code}]: {resp.text}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise AuthenticationError(f"PA UJS OAuth network error: {exc}") from exc

        token_data = resp.json()
        self._access_token = token_data["access_token"]
        expires_in = int(token_data.get("expires_in", 3600))
        self._token_expiry = datetime.fromtimestamp(
            datetime.utcnow().timestamp() + expires_in, tz=timezone.utc
        )
        logger.info(
            "PA UJS authentication successful",
            extra={"expires_in_seconds": expires_in, "adapter": self.get_adapter_id()},
        )

    def fetch_new_filings(self, since_timestamp: datetime) -> List[Dict[str, Any]]:
        """
        Fetch all LT/eviction filings filed after since_timestamp.
        Handles pagination via OData-style $skip/$top parameters.
        Returns a flat list of raw dicts.
        """
        self._maybe_refresh_token()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        results: List[Dict[str, Any]] = []
        skip = 0

        while True:
            params: Dict[str, Any] = {
                "$filter": (
                    f"FilingDate gt '{since_timestamp.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                    f"and CaseType in ('LANDLORD-TENANT','LT-EVICTION','MD-LT')"
                ),
                "$orderby": "FilingDate asc",
                "$top": self._page_size,
                "$skip": skip,
                "$select": (
                    "CaseId,DocketNumber,FilingDate,CourtName,JurisdictionCode,"
                    "PlaintiffName,PlaintiffAddress,DefendantName,PropertyAddress,"
                    "ZipCode,FilingReason,ClaimedAmount,NoticeDate,NoticeType,"
                    "HearingDate,DocumentId,DocumentFormat"
                ),
            }
            try:
                resp = requests.get(
                    _PA_FILINGS_URL,
                    headers=headers,
                    params=params,
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                if resp.status_code in (401, 403):
                    # Force re-auth on next retry
                    self._access_token = None
                raise FetchError(
                    f"PA UJS filing fetch failed [{resp.status_code}]: {resp.text}"
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise FetchError(f"PA UJS network error during fetch: {exc}") from exc

            batch = resp.json()
            if isinstance(batch, dict):
                # OData envelope: { "value": [...], "@odata.nextLink": "..." }
                items = batch.get("value", [])
            else:
                items = batch

            results.extend(items)

            logger.info(
                "PA UJS filing page fetched",
                extra={
                    "page_skip": skip,
                    "page_count": len(items),
                    "cumulative": len(results),
                },
            )

            if len(items) < self._page_size:
                break  # Last page
            skip += self._page_size

        return results

    def parse_filing_response(self, raw_response: Any) -> Dict[str, Any]:
        """
        Map a single PA UJS raw filing dict to the intermediate representation.
        Performs field-level coercion and basic validation.
        """
        if not isinstance(raw_response, dict):
            raise ParseError(f"Expected dict, got {type(raw_response)}")

        required = ("CaseId", "DocketNumber", "FilingDate", "PlaintiffName", "PropertyAddress")
        missing = [k for k in required if not raw_response.get(k)]
        if missing:
            raise ParseError(f"PA UJS response missing required fields: {missing}")

        def _parse_date(s: Optional[str]) -> Optional[datetime]:
            if not s:
                return None
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
            logger.warning("Could not parse date string: %s", s)
            return None

        def _safe_float(v: Any) -> Optional[float]:
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        raw_notice_type = raw_response.get("NoticeType", "")
        normalized_notice = _NOTICE_TYPE_MAP.get(
            str(raw_notice_type).upper(), raw_notice_type.lower() if raw_notice_type else None
        )

        doc_format_raw = str(raw_response.get("DocumentFormat", "PDF")).lower()
        try:
            doc_format = DocumentFormat(doc_format_raw)
        except ValueError:
            doc_format = DocumentFormat.PDF  # Default to PDF for PA

        return {
            "source_case_id": str(raw_response["CaseId"]),
            "case_number": self._normalize_docket(raw_response["DocketNumber"]),
            "court_name": raw_response.get("CourtName", "Magisterial District Court"),
            "jurisdiction_code": raw_response.get("JurisdictionCode", self.jurisdiction_code),
            "landlord_name": str(raw_response["PlaintiffName"]).strip(),
            "landlord_address": raw_response.get("PlaintiffAddress"),
            "tenant_name": raw_response.get("DefendantName"),
            "property_address": str(raw_response["PropertyAddress"]).strip(),
            "zip_code": self._extract_zip(raw_response.get("ZipCode", "")),
            "filing_date": _parse_date(raw_response.get("FilingDate")),
            "filing_reason": raw_response.get("FilingReason", "Non-payment of rent"),
            "claimed_amount": _safe_float(raw_response.get("ClaimedAmount")),
            "notice_date": _parse_date(raw_response.get("NoticeDate")),
            "notice_type": normalized_notice,
            "hearing_date": _parse_date(raw_response.get("HearingDate")),
            "document_id": raw_response.get("DocumentId"),
            "document_format": doc_format,
            "raw_download_url": (
                _PA_DOCUMENT_URL.format(doc_id=raw_response["DocumentId"])
                if raw_response.get("DocumentId")
                else None
            ),
        }

    def normalize_to_schema(
        self, parsed_filing: Dict[str, Any], document_uri: str
    ) -> NormalizedFiling:
        """Produce a NormalizedFiling from the parsed intermediate dict."""
        return NormalizedFiling(
            source_case_id=parsed_filing["source_case_id"],
            case_number=parsed_filing["case_number"],
            jurisdiction_code=parsed_filing["jurisdiction_code"],
            court_name=parsed_filing["court_name"],
            state="PA",
            landlord_name=parsed_filing["landlord_name"],
            landlord_address=parsed_filing.get("landlord_address"),
            tenant_name=parsed_filing.get("tenant_name"),
            property_address=parsed_filing["property_address"],
            zip_code=parsed_filing["zip_code"],
            filing_date=parsed_filing["filing_date"],
            filing_reason=parsed_filing["filing_reason"],
            claimed_amount=parsed_filing.get("claimed_amount"),
            notice_date=parsed_filing.get("notice_date"),
            notice_type=parsed_filing.get("notice_type"),
            hearing_date=parsed_filing.get("hearing_date"),
            document_format=parsed_filing["document_format"],
            document_uri=document_uri,
            raw_download_url=parsed_filing.get("raw_download_url"),
        )

    # ------------------------------------------------------------------
    # PA-specific helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_docket(raw: str) -> str:
        """
        Normalize PA docket numbers to the format: PA-MDJ-YYYY-NNNNNN
        Raw PA format examples:
            MJ-05201-LT-0000123-2024
            LT-05201-2024-123
        """
        digits = re.findall(r"\d+", raw)
        if len(digits) >= 3:
            year = next((d for d in digits if len(d) == 4 and d.startswith("20")), digits[-1])
            seq = digits[-1].zfill(6)
            return f"PA-MDJ-{year}-{seq}"
        return f"PA-MDJ-{raw}"

    @staticmethod
    def _extract_zip(raw: str) -> str:
        """Extract 5-digit ZIP from any format (e.g., '19103-1234' → '19103')."""
        match = re.search(r"\b(\d{5})\b", str(raw))
        return match.group(1) if match else raw[:5] if raw else "00000"

    def download_document(self, download_url: str) -> bytes:
        """
        Download a court document binary. Uses bearer auth.
        Caller is responsible for retry; this method does one attempt.
        """
        self._maybe_refresh_token()
        resp = requests.get(
            download_url,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=60,
            stream=False,
        )
        resp.raise_for_status()
        return resp.content

    def upload_to_gcs(self, data: bytes, case_number: str, doc_format: DocumentFormat) -> str:
        """
        Upload raw document bytes to Cloud Storage.
        Returns the gs:// URI.
        """
        ext = doc_format.value
        blob_name = f"raw-filings/pa/{datetime.utcnow().strftime('%Y/%m/%d')}/{case_number}.{ext}"
        bucket = self._storage_client.bucket(self._gcs_bucket_raw)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(data, content_type=f"application/{ext}")
        uri = f"gs://{self._gcs_bucket_raw}/{blob_name}"
        logger.info("Uploaded filing to GCS", extra={"uri": uri, "bytes": len(data)})
        return uri
