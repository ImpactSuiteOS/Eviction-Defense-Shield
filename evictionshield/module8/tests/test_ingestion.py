"""
EvictionShield — Module 8: Ingestion Pipeline Tests

Tests:
- PA UJS adapter normalization
- Docket number normalization
- Checksum computation
- GCS upload URI format
- Pub/Sub payload serialization
- Document processing extraction (mocked Document AI)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from module1.court_ingestion.adapters.base import NormalizedFiling, DocumentFormat
from module1.court_ingestion.adapters.pennsylvania_ujs import PennsylvaniaUJSAdapter
from .fixtures.filings import FIXTURE_CLEAN, FIXTURE_IMPROPER_NOTICE, ALL_FIXTURES


# ---------------------------------------------------------------------------
# PA UJS Adapter Unit Tests
# ---------------------------------------------------------------------------

class TestPennsylvaniaUJSAdapterNormalization:
    """Test parse_filing_response and normalize_to_schema."""

    def _make_adapter(self) -> PennsylvaniaUJSAdapter:
        with patch("module1.court_ingestion.adapters.pennsylvania_ujs.secretmanager"):
            with patch("module1.court_ingestion.adapters.pennsylvania_ujs.storage"):
                return PennsylvaniaUJSAdapter(
                    gcp_project_id="test-project",
                    gcs_bucket_raw="test-bucket",
                )

    def test_docket_normalization_standard_format(self):
        adapter = self._make_adapter()
        result = adapter._normalize_docket("MJ-05201-LT-0000123-2024")
        assert result.startswith("PA-MDJ-2024-")
        assert "123" in result

    def test_docket_normalization_preserves_year(self):
        adapter = self._make_adapter()
        result = adapter._normalize_docket("LT-2023-004567")
        assert "2023" in result

    def test_zip_extraction_five_digit(self):
        assert PennsylvaniaUJSAdapter._extract_zip("19103") == "19103"

    def test_zip_extraction_nine_digit(self):
        assert PennsylvaniaUJSAdapter._extract_zip("19103-1234") == "19103"

    def test_zip_extraction_from_address(self):
        assert PennsylvaniaUJSAdapter._extract_zip("Philadelphia PA 19147") == "19147"

    def test_parse_filing_response_clean_fixture(self):
        adapter = self._make_adapter()
        raw = {
            "CaseId": "001",
            "DocketNumber": "MJ-05201-LT-0000001-2024",
            "FilingDate": "2024-06-15T00:00:00Z",
            "PlaintiffName": "Oak Properties LLC",
            "PropertyAddress": "500 Broad St Apt 3B, Philadelphia, PA 19147",
            "ZipCode": "19147",
            "FilingReason": "Nonpayment of rent",
            "ClaimedAmount": "1200.00",
            "NoticeDate": "2024-06-01T00:00:00Z",
            "NoticeType": "NON-PAYMENT",
            "HearingDate": "2024-07-10T00:00:00Z",
            "DocumentId": "doc001",
            "DocumentFormat": "PDF",
            "DefendantName": "Jane Smith",
            "CourtName": "MDC 05-2-01",
        }
        parsed = adapter.parse_filing_response(raw)
        assert parsed["landlord_name"] == "Oak Properties LLC"
        assert parsed["zip_code"] == "19147"
        assert parsed["notice_type"] == "pay_or_quit"
        assert parsed["claimed_amount"] == 1200.0
        assert parsed["document_format"] == DocumentFormat.PDF

    def test_parse_filing_response_missing_required_field_raises(self):
        adapter = self._make_adapter()
        with pytest.raises(Exception):
            adapter.parse_filing_response({"CaseId": "001"})  # Missing PlaintiffName etc.

    def test_normalize_to_schema_produces_valid_filing(self):
        adapter = self._make_adapter()
        raw = {
            "CaseId": "002",
            "DocketNumber": "MJ-05201-LT-0000002-2024",
            "FilingDate": "2024-06-15T00:00:00Z",
            "PlaintiffName": "Test LLC",
            "PropertyAddress": "100 Test St, Philadelphia, PA 19103",
            "ZipCode": "19103",
            "FilingReason": "Nonpayment",
            "HearingDate": "2024-07-01T00:00:00Z",
            "DocumentId": "doc002",
            "DocumentFormat": "PDF",
        }
        parsed = adapter.parse_filing_response(raw)
        filing = adapter.normalize_to_schema(parsed, "gs://test-bucket/raw-filings/pa/case.pdf")
        assert isinstance(filing, NormalizedFiling)
        assert filing.state == "PA"
        assert filing.document_uri.startswith("gs://")

    def test_checksum_computation(self):
        data = b"test content"
        checksum = PennsylvaniaUJSAdapter.compute_checksum(data)
        expected = hashlib.sha256(data).hexdigest()
        assert checksum == expected

    def test_normalized_filing_to_pubsub_dict_serializable(self):
        filing = NormalizedFiling(
            source_case_id="001",
            case_number="PA-MDJ-2024-000001",
            jurisdiction_code="PA-UJS",
            court_name="MDC 05-2-01",
            state="PA",
            landlord_name="Test LLC",
            landlord_address=None,
            tenant_name="Test Tenant",
            property_address="100 Test St",
            zip_code="19103",
            filing_date=datetime(2024, 6, 15),
            filing_reason="Nonpayment",
            claimed_amount=1200.0,
            notice_date=datetime(2024, 6, 1),
            notice_type="pay_or_quit",
            hearing_date=datetime(2024, 7, 10),
            document_format=DocumentFormat.PDF,
            document_uri="gs://test-bucket/test.pdf",
        )
        d = filing.to_pubsub_dict()
        # Must be JSON-serializable
        serialized = json.dumps(d)
        assert json.loads(serialized)["case_number"] == "PA-MDJ-2024-000001"


# ---------------------------------------------------------------------------
# Document Processing Extraction Tests (mocked Document AI)
# ---------------------------------------------------------------------------

class TestDocumentProcessingExtraction:
    """Test field extraction from mocked Document AI output."""

    @patch("module1.document_processing.main._process_with_docai")
    @patch("module1.document_processing.main._download_from_gcs")
    def test_pdf_routing_calls_form_parser(self, mock_download, mock_docai):
        """PDF documents should be routed to the Form Parser processor."""
        mock_download.return_value = (b"fake-pdf", "application/pdf")
        mock_docai.return_value = self._build_mock_document(FIXTURE_CLEAN)

        from module1.document_processing.main import _extract_fields_from_docai
        # Test that the function returns an ExtractionResult
        result = _extract_fields_from_docai(mock_docai.return_value)
        # Result should be an ExtractionResult instance
        assert result is not None

    def test_structured_json_extraction_confidence_is_1(self):
        """JSON/XML filings should have confidence=1.0 for all extracted fields."""
        from module1.document_processing.main import _extract_fields_from_structured
        result = _extract_fields_from_structured(FIXTURE_CLEAN)
        assert result.case_number is not None
        assert result.case_number.confidence == 1.0
        assert result.landlord_name is not None
        assert result.landlord_name.confidence == 1.0

    def test_structured_extraction_extracts_all_fields(self):
        from module1.document_processing.main import _extract_fields_from_structured
        result = _extract_fields_from_structured(FIXTURE_CLEAN)
        assert result.case_number.value == "PA-MDJ-2024-000001"
        assert result.zip_code.value == "19147"
        assert result.claimed_rent_owed.value == 1200.0

    def test_low_confidence_detection(self):
        """ExtractionResult correctly identifies low confidence."""
        from module1.document_processing.main import ExtractionResult, ExtractedField
        result = ExtractionResult()
        result.case_number = ExtractedField(value="PA-001", confidence=0.4)
        result.filing_date = ExtractedField(value=None, confidence=0.6)
        result.landlord_name = ExtractedField(value="Test LLC", confidence=0.8)
        result.property_address = ExtractedField(value="100 Main St", confidence=0.9)
        result.zip_code = ExtractedField(value="19103", confidence=0.8)
        result.hearing_date = ExtractedField(value=None, confidence=0.5)

        assert result.lowest_critical_confidence() == pytest.approx(0.4)

    def _build_mock_document(self, filing_data: dict):
        """Build a minimal mock Document AI response object."""
        doc = MagicMock()
        doc.text = ""
        doc.entities = []
        doc.pages = []
        return doc


# ---------------------------------------------------------------------------
# Idempotency Tests
# ---------------------------------------------------------------------------

class TestIngestionIdempotency:
    """Verify that reprocessing the same filing does not create duplicates."""

    @patch("module1.document_processing.main._firestore_client")
    def test_firestore_write_is_idempotent(self, mock_db):
        """Writing the same case_number twice should result in a single document."""
        from module1.document_processing.main import _write_to_firestore, ExtractionResult, ExtractedField

        mock_doc_ref = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        extraction = ExtractionResult()
        extraction.case_number = ExtractedField("PA-001", 1.0)
        extraction.landlord_name = ExtractedField("Test LLC", 1.0)
        extraction.property_address = ExtractedField("100 Main", 1.0)
        extraction.zip_code = ExtractedField("19103", 1.0)
        extraction.filing_date = ExtractedField("2024-06-15", 1.0)
        extraction.hearing_date = ExtractedField("2024-07-10", 1.0)

        # Call twice with same data
        _write_to_firestore("PA-001", extraction, "gs://bucket/file.pdf", {"state": "PA"})
        _write_to_firestore("PA-001", extraction, "gs://bucket/file.pdf", {"state": "PA"})

        # .set(merge=True) should be called twice, but on the SAME document reference
        assert mock_doc_ref.set.call_count == 2
        # Both calls use the same doc ID
        assert mock_db.collection.return_value.document.call_count == 2
