"""
EvictionShield — Module 1A: Court API Adapter Base Class
Abstract base that every state-specific adapter must implement.
"""

from __future__ import annotations

import abc
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DocumentFormat(str, Enum):
    PDF = "pdf"
    XML = "xml"
    CSV = "csv"
    JSON = "json"
    HTML = "html"


@dataclass
class NormalizedFiling:
    """
    Canonical schema every adapter must produce.
    Downstream services depend only on this structure — never on raw court data.
    """
    # Identifiers
    source_case_id: str                     # Raw ID from the court system
    case_number: str                        # Normalized case number (state-prefixed)
    jurisdiction_code: str                  # e.g. "PA-MDJ-05-2-01"
    court_name: str
    state: str                              # Two-letter state code

    # Parties
    landlord_name: str
    landlord_address: Optional[str]
    tenant_name: Optional[str]              # Some jurisdictions redact
    property_address: str
    zip_code: str

    # Filing metadata
    filing_date: datetime
    filing_reason: str
    claimed_amount: Optional[float]
    notice_date: Optional[datetime]
    notice_type: Optional[str]
    hearing_date: Optional[datetime]

    # Document
    document_format: DocumentFormat
    document_uri: str                       # gs:// URI after upload to Cloud Storage
    raw_download_url: Optional[str] = None  # Ephemeral — cleared after upload

    # Ingestion bookkeeping
    ingestion_timestamp: datetime = field(default_factory=datetime.utcnow)
    adapter_version: str = "1.0.0"
    checksum_sha256: Optional[str] = None   # Set after file download

    def to_pubsub_dict(self) -> Dict[str, Any]:
        """Serialise for Pub/Sub message body (JSON-safe)."""
        d = {
            "source_case_id": self.source_case_id,
            "case_number": self.case_number,
            "jurisdiction_code": self.jurisdiction_code,
            "court_name": self.court_name,
            "state": self.state,
            "landlord_name": self.landlord_name,
            "landlord_address": self.landlord_address,
            "tenant_name": self.tenant_name,
            "property_address": self.property_address,
            "zip_code": self.zip_code,
            "filing_date": self.filing_date.isoformat() if self.filing_date else None,
            "filing_reason": self.filing_reason,
            "claimed_amount": self.claimed_amount,
            "notice_date": self.notice_date.isoformat() if self.notice_date else None,
            "notice_type": self.notice_type,
            "hearing_date": self.hearing_date.isoformat() if self.hearing_date else None,
            "document_format": self.document_format.value,
            "document_uri": self.document_uri,
            "ingestion_timestamp": self.ingestion_timestamp.isoformat(),
            "adapter_version": self.adapter_version,
            "checksum_sha256": self.checksum_sha256,
        }
        return d


class AuthenticationError(Exception):
    """Raised when the adapter cannot obtain a valid auth token."""


class FetchError(Exception):
    """Raised on non-retryable API fetch failures."""


class ParseError(Exception):
    """Raised when the raw API response cannot be parsed."""


class CourtAPIAdapter(abc.ABC):
    """
    Abstract base class for all state court API connectors.

    Subclass this and implement all four abstract methods.
    The ingestion service calls them in the order:
        authenticate() → fetch_new_filings() → parse_filing_response() → normalize_to_schema()
    """

    def __init__(self, state_code: str, jurisdiction_code: str):
        self.state_code = state_code
        self.jurisdiction_code = jurisdiction_code
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Abstract interface — every adapter must implement these four methods
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def authenticate(self) -> None:
        """
        Obtain and store an access token.

        Must:
        - Read credentials from Google Secret Manager (never from env vars or code)
        - Store the token in self._access_token
        - Store expiry in self._token_expiry
        - Raise AuthenticationError on failure

        Example credential secret name convention:
            court-api-creds-{state_code.lower()}
        """

    @abc.abstractmethod
    def fetch_new_filings(self, since_timestamp: datetime) -> List[Dict[str, Any]]:
        """
        Return a list of raw filing records created after since_timestamp.

        Must:
        - Refresh the auth token if expired (call self._maybe_refresh_token())
        - Handle pagination internally — always return a flat list
        - Include the raw download URL for each filing document
        - Raise FetchError on non-retryable failures
        - Return [] if no new filings (never raise on empty result)

        Args:
            since_timestamp: Only return filings created strictly after this time.

        Returns:
            List of raw dict records as returned by the court API.
        """

    @abc.abstractmethod
    def parse_filing_response(self, raw_response: Any) -> Dict[str, Any]:
        """
        Parse a single raw filing record into an intermediate dict.

        The intermediate dict must contain at minimum:
            source_case_id, landlord_name, property_address, zip_code,
            filing_date, hearing_date, document_url, document_format

        Args:
            raw_response: A single element from the list returned by fetch_new_filings().

        Returns:
            Intermediate dict with all available fields populated.
            Raise ParseError if the response is malformed beyond recovery.
        """

    @abc.abstractmethod
    def normalize_to_schema(self, parsed_filing: Dict[str, Any], document_uri: str) -> NormalizedFiling:
        """
        Map the parsed intermediate dict to a NormalizedFiling.

        Args:
            parsed_filing:  Output of parse_filing_response().
            document_uri:   gs:// URI — the document has already been uploaded to GCS.

        Returns:
            A fully populated NormalizedFiling instance.
            All required fields must be set; optional fields may be None.
        """

    # ------------------------------------------------------------------
    # Concrete helper methods shared by all adapters
    # ------------------------------------------------------------------

    def is_token_valid(self) -> bool:
        """Return True if the stored access token is still usable."""
        if not self._access_token or not self._token_expiry:
            return False
        # Subtract 60 seconds to avoid using a token right on the edge of expiry
        return datetime.utcnow().timestamp() < (self._token_expiry.timestamp() - 60)

    def _maybe_refresh_token(self) -> None:
        """Re-authenticate if the current token is missing or about to expire."""
        if not self.is_token_valid():
            logger.info(
                "Access token missing or expiring; re-authenticating",
                extra={"state": self.state_code, "jurisdiction": self.jurisdiction_code},
            )
            self.authenticate()

    @staticmethod
    def compute_checksum(data: bytes) -> str:
        """Return the SHA-256 hex digest of raw bytes."""
        return hashlib.sha256(data).hexdigest()

    def get_adapter_id(self) -> str:
        """Stable string ID used in logs and metrics."""
        return f"{self.state_code.upper()}-{self.jurisdiction_code}"
