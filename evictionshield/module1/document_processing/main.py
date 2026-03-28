"""
EvictionShield — Module 1B: Document Processing Cloud Function

Triggered by Pub/Sub messages on `raw-eviction-filings`.
Routes each document to the correct Document AI processor, extracts structured
fields with confidence scores, writes to Firestore, and re-publishes to
`structured-filings-ready` (or `low-confidence-filings` on low confidence).

Environment variables:
    GCP_PROJECT_ID              Google Cloud project ID
    GCP_LOCATION                Document AI location (default: us)
    DOCAI_FORM_PARSER_ID        Document AI Form Parser processor ID
    DOCAI_OCR_PROCESSOR_ID      Document AI OCR processor ID
    PUBSUB_TOPIC_STRUCTURED     Pub/Sub topic for confident extractions (default: structured-filings-ready)
    PUBSUB_TOPIC_LOW_CONF       Pub/Sub topic for low-confidence cases (default: low-confidence-filings)
    CONFIDENCE_THRESHOLD        Float 0-1 (default: 0.75)
    FIRESTORE_COLLECTION        Firestore collection name (default: eviction_filings)
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import functions_framework
from google.cloud import documentai_v1 as documentai
from google.cloud import firestore, pubsub_v1, storage
from google.cloud.documentai_v1.types import Document

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
LOCATION: str = os.environ.get("GCP_LOCATION", "us")
FORM_PARSER_ID: str = os.environ["DOCAI_FORM_PARSER_ID"]
OCR_PROCESSOR_ID: str = os.environ["DOCAI_OCR_PROCESSOR_ID"]
PUBSUB_TOPIC_STRUCTURED: str = os.environ.get("PUBSUB_TOPIC_STRUCTURED", "structured-filings-ready")
PUBSUB_TOPIC_LOW_CONF: str = os.environ.get("PUBSUB_TOPIC_LOW_CONF", "low-confidence-filings")
CONFIDENCE_THRESHOLD: float = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.75"))
FIRESTORE_COLLECTION: str = os.environ.get("FIRESTORE_COLLECTION", "eviction_filings")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Clients (module-level singletons for Cloud Function warm reuse)
# ---------------------------------------------------------------------------

_docai_client = documentai.DocumentProcessorServiceClient()
_firestore_client = firestore.Client(project=PROJECT_ID)
_storage_client = storage.Client(project=PROJECT_ID)
_pubsub_publisher = pubsub_v1.PublisherClient()

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ExtractedField:
    value: Any
    confidence: float
    raw_text: str = ""


@dataclass
class ExtractionResult:
    case_number: Optional[ExtractedField] = None
    filing_date: Optional[ExtractedField] = None
    landlord_name: Optional[ExtractedField] = None
    landlord_entity_type: Optional[ExtractedField] = None
    tenant_name: Optional[ExtractedField] = None
    property_address: Optional[ExtractedField] = None
    zip_code: Optional[ExtractedField] = None
    claimed_rent_owed: Optional[ExtractedField] = None
    stated_filing_reason: Optional[ExtractedField] = None
    notice_date: Optional[ExtractedField] = None
    notice_type: Optional[ExtractedField] = None
    hearing_date: Optional[ExtractedField] = None
    attorney_for_landlord: Optional[ExtractedField] = None

    # Non-critical fields excluded from confidence gating
    CRITICAL_FIELDS: frozenset = field(default_factory=lambda: frozenset({
        "case_number", "filing_date", "landlord_name",
        "property_address", "zip_code", "hearing_date",
    }))

    def to_firestore_dict(self) -> Dict[str, Any]:
        """Flatten to Firestore-ready dict with _confidence suffixed metadata."""
        out: Dict[str, Any] = {}
        for fname in self.__dataclass_fields__:
            if fname == "CRITICAL_FIELDS":
                continue
            ef: Optional[ExtractedField] = getattr(self, fname)
            out[fname] = ef.value if ef else None
            out[f"{fname}_confidence"] = ef.confidence if ef else None
        return out

    def lowest_critical_confidence(self) -> float:
        """Return the minimum confidence across all critical fields that were extracted."""
        scores = []
        for fname in self.CRITICAL_FIELDS:
            ef: Optional[ExtractedField] = getattr(self, fname)
            if ef is not None:
                scores.append(ef.confidence)
        return min(scores) if scores else 0.0

    def missing_critical_fields(self) -> List[str]:
        return [f for f in self.CRITICAL_FIELDS if getattr(self, f) is None]


# ---------------------------------------------------------------------------
# Document routing
# ---------------------------------------------------------------------------

def _processor_name(processor_id: str) -> str:
    return f"projects/{PROJECT_ID}/locations/{LOCATION}/processors/{processor_id}"


def _download_from_gcs(uri: str) -> Tuple[bytes, str]:
    """
    Download raw bytes from a gs:// URI.
    Returns (bytes, mime_type).
    """
    assert uri.startswith("gs://"), f"Expected gs:// URI, got: {uri}"
    path = uri[5:]
    bucket_name, blob_name = path.split("/", 1)
    bucket = _storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    data = blob.download_as_bytes()
    content_type = blob.content_type or _infer_mime(blob_name)
    return data, content_type


def _infer_mime(blob_name: str) -> str:
    ext = blob_name.rsplit(".", 1)[-1].lower()
    return {
        "pdf": "application/pdf",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "tiff": "image/tiff",
        "xml": "application/xml",
        "json": "application/json",
        "csv": "text/csv",
    }.get(ext, "application/octet-stream")


def _process_with_docai(
    raw_bytes: bytes,
    mime_type: str,
    processor_id: str,
) -> Document:
    """
    Send a document to Document AI and return the parsed Document object.
    """
    processor_name = _processor_name(processor_id)
    request = documentai.ProcessRequest(
        name=processor_name,
        raw_document=documentai.RawDocument(content=raw_bytes, mime_type=mime_type),
    )
    result = _docai_client.process_document(request=request)
    return result.document


# ---------------------------------------------------------------------------
# Field extraction from Document AI output
# ---------------------------------------------------------------------------

# Mapping from Document AI entity type names → our field names
_ENTITY_FIELD_MAP: Dict[str, str] = {
    # Pennsylvania-specific form parser trained entity names
    "case_number": "case_number",
    "docket_number": "case_number",
    "filing_date": "filing_date",
    "date_filed": "filing_date",
    "plaintiff": "landlord_name",
    "plaintiff_name": "landlord_name",
    "defendant": "tenant_name",
    "defendant_name": "tenant_name",
    "property_address": "property_address",
    "rental_address": "property_address",
    "zip_code": "zip_code",
    "zip": "zip_code",
    "amount_claimed": "claimed_rent_owed",
    "rent_owed": "claimed_rent_owed",
    "filing_reason": "stated_filing_reason",
    "reason": "stated_filing_reason",
    "notice_date": "notice_date",
    "notice_type": "notice_type",
    "hearing_date": "hearing_date",
    "scheduled_hearing_date": "hearing_date",
    "attorney": "attorney_for_landlord",
    "plaintiff_attorney": "attorney_for_landlord",
}

_ENTITY_TYPE_KEYWORDS: Dict[str, str] = {
    "llc": "LLC",
    "l.l.c": "LLC",
    "inc": "corporation",
    "corp": "corporation",
    "lp": "LLC",
    "l.p.": "LLC",
    "property management": "property_mgmt_company",
    "realty": "property_mgmt_company",
    "properties": "property_mgmt_company",
}


def _classify_landlord_entity(name: str) -> str:
    lower = name.lower()
    for kw, entity_type in _ENTITY_TYPE_KEYWORDS.items():
        if kw in lower:
            return entity_type
    return "individual"


def _parse_date_value(raw: str) -> Optional[date]:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_currency(raw: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d.]", "", raw)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_zip_from_address(addr: str) -> Optional[str]:
    match = re.search(r"\b(\d{5})\b", addr)
    return match.group(1) if match else None


def _extract_fields_from_docai(document: Document) -> ExtractionResult:
    """
    Walk Document AI entities and form fields to populate ExtractionResult.
    Handles both Form Parser (key-value pairs) and general OCR (entities).
    """
    result = ExtractionResult()

    # --- Named entities (from trained processor or general NLP) ---
    for entity in document.entities:
        raw_type = entity.type_.lower().replace(" ", "_")
        field_name = _ENTITY_FIELD_MAP.get(raw_type)
        if not field_name:
            continue

        raw_text = entity.mention_text.strip()
        confidence = float(entity.confidence) if entity.confidence else 0.5
        value: Any = raw_text

        # Type coercions
        if field_name in ("filing_date", "notice_date", "hearing_date"):
            value = _parse_date_value(raw_text)
        elif field_name == "claimed_rent_owed":
            value = _parse_currency(raw_text)
        elif field_name == "zip_code":
            match = re.search(r"\b(\d{5})\b", raw_text)
            value = match.group(1) if match else raw_text

        if getattr(result, field_name) is None or confidence > getattr(result, field_name).confidence:
            setattr(result, field_name, ExtractedField(value=value, confidence=confidence, raw_text=raw_text))

    # --- Form fields (Form Parser only) ---
    for page in document.pages:
        for field_data in page.form_fields:
            key_raw = _get_text(document, field_data.field_name).strip().lower()
            val_raw = _get_text(document, field_data.field_value).strip()
            key_normalized = re.sub(r"[\s:_-]+", "_", key_raw)
            field_name = _ENTITY_FIELD_MAP.get(key_normalized)
            if not field_name or not val_raw:
                continue

            confidence = float(field_data.field_value.confidence) if field_data.field_value.confidence else 0.5
            value = val_raw

            if field_name in ("filing_date", "notice_date", "hearing_date"):
                value = _parse_date_value(val_raw)
            elif field_name == "claimed_rent_owed":
                value = _parse_currency(val_raw)

            existing: Optional[ExtractedField] = getattr(result, field_name)
            if existing is None or confidence > existing.confidence:
                setattr(result, field_name, ExtractedField(value=value, confidence=confidence, raw_text=val_raw))

    # --- Derive zip from property_address if not found ---
    if result.zip_code is None and result.property_address is not None:
        z = _extract_zip_from_address(str(result.property_address.value))
        if z:
            result.zip_code = ExtractedField(value=z, confidence=result.property_address.confidence * 0.9, raw_text=z)

    # --- Derive landlord_entity_type ---
    if result.landlord_name is not None and result.landlord_entity_type is None:
        entity_type = _classify_landlord_entity(str(result.landlord_name.value))
        result.landlord_entity_type = ExtractedField(
            value=entity_type, confidence=0.8, raw_text=entity_type
        )

    return result


def _get_text(document: Document, field_ref: Any) -> str:
    """Extract text from a Document AI text anchor reference."""
    if not field_ref or not field_ref.text_anchor:
        return ""
    return "".join(
        document.text[seg.start_index: seg.end_index]
        for seg in field_ref.text_anchor.text_segments
    )


# ---------------------------------------------------------------------------
# Direct schema mapping for XML/JSON filings
# ---------------------------------------------------------------------------

def _extract_fields_from_structured(raw_msg: Dict[str, Any]) -> ExtractionResult:
    """
    For filings that arrived as structured XML/JSON (already parsed by the
    ingestion adapter), map NormalizedFiling fields directly.
    All fields have confidence 1.0 since they came from a structured source.
    """

    def ef(val: Any, conf: float = 1.0) -> Optional[ExtractedField]:
        if val is None:
            return None
        return ExtractedField(value=val, confidence=conf, raw_text=str(val))

    def parse_date(s: Optional[str]) -> Optional[date]:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s).date()
        except ValueError:
            return _parse_date_value(s)

    result = ExtractionResult()
    result.case_number = ef(raw_msg.get("case_number"))
    result.filing_date = ef(parse_date(raw_msg.get("filing_date")))
    result.landlord_name = ef(raw_msg.get("landlord_name"))
    result.tenant_name = ef(raw_msg.get("tenant_name"))
    result.property_address = ef(raw_msg.get("property_address"))
    result.zip_code = ef(raw_msg.get("zip_code"))
    result.claimed_rent_owed = ef(
        float(raw_msg["claimed_amount"]) if raw_msg.get("claimed_amount") else None
    )
    result.stated_filing_reason = ef(raw_msg.get("filing_reason"))
    result.notice_date = ef(parse_date(raw_msg.get("notice_date")))
    result.notice_type = ef(raw_msg.get("notice_type"))
    result.hearing_date = ef(parse_date(raw_msg.get("hearing_date")))

    if result.landlord_name:
        entity_type = _classify_landlord_entity(str(result.landlord_name.value))
        result.landlord_entity_type = ef(entity_type, 0.85)

    return result


# ---------------------------------------------------------------------------
# Firestore write (idempotent)
# ---------------------------------------------------------------------------

def _write_to_firestore(
    case_number: str,
    extraction: ExtractionResult,
    document_uri: str,
    raw_msg: Dict[str, Any],
) -> None:
    """
    Write (or merge) the extraction result to Firestore.
    Uses case_number as the document ID → idempotent re-runs are safe.
    """
    doc_ref = _firestore_client.collection(FIRESTORE_COLLECTION).document(
        case_number.replace("/", "_")
    )
    data = extraction.to_firestore_dict()
    data["document_uri"] = document_uri
    data["state"] = raw_msg.get("state")
    data["jurisdiction_code"] = raw_msg.get("jurisdiction_code")
    data["ingestion_timestamp"] = raw_msg.get("ingestion_timestamp")
    data["extraction_timestamp"] = datetime.utcnow().isoformat()
    data["processing_status"] = "extracted"

    doc_ref.set(data, merge=True)
    logger.info("Wrote extraction to Firestore: %s", case_number)


# ---------------------------------------------------------------------------
# Pub/Sub publishing
# ---------------------------------------------------------------------------

def _publish_result(topic_name: str, payload: Dict[str, Any], case_number: str) -> None:
    topic_path = f"projects/{PROJECT_ID}/topics/{topic_name}"
    data = json.dumps(payload, default=str).encode("utf-8")
    future = _pubsub_publisher.publish(
        topic_path, data=data, case_number=case_number
    )
    future.result(timeout=30)
    logger.info("Published to %s: %s", topic_name, case_number)


# ---------------------------------------------------------------------------
# Cloud Function entrypoint
# ---------------------------------------------------------------------------

@functions_framework.cloud_event
def process_filing(cloud_event) -> None:
    """
    Cloud Function entrypoint — triggered by Pub/Sub `raw-eviction-filings`.
    """
    # Decode Pub/Sub message
    pubsub_data = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
    raw_msg: Dict[str, Any] = json.loads(pubsub_data)

    case_number: str = raw_msg.get("case_number", "UNKNOWN")
    document_uri: str = raw_msg.get("document_uri", "")
    doc_format: str = raw_msg.get("document_format", "pdf").lower()

    logger.info(
        json.dumps({
            "severity": "INFO",
            "message": "Processing filing",
            "case_number": case_number,
            "document_format": doc_format,
            "document_uri": document_uri,
        })
    )

    try:
        # -------------------------------------------------------------------
        # Route by document type
        # -------------------------------------------------------------------
        if doc_format in ("json", "xml", "csv"):
            # Structured data — direct schema mapping, no Document AI needed
            extraction = _extract_fields_from_structured(raw_msg)

        elif doc_format == "pdf":
            raw_bytes, mime_type = _download_from_gcs(document_uri)
            document = _process_with_docai(raw_bytes, mime_type, FORM_PARSER_ID)
            extraction = _extract_fields_from_docai(document)

        elif doc_format in ("png", "jpg", "jpeg", "tiff"):
            # Scanned image — use OCR processor
            raw_bytes, mime_type = _download_from_gcs(document_uri)
            document = _process_with_docai(raw_bytes, mime_type, OCR_PROCESSOR_ID)
            extraction = _extract_fields_from_docai(document)

        else:
            # Attempt PDF as fallback
            logger.warning("Unknown document format '%s', attempting PDF processing", doc_format)
            raw_bytes, mime_type = _download_from_gcs(document_uri)
            document = _process_with_docai(raw_bytes, "application/pdf", FORM_PARSER_ID)
            extraction = _extract_fields_from_docai(document)

        # -------------------------------------------------------------------
        # Confidence gate
        # -------------------------------------------------------------------
        min_conf = extraction.lowest_critical_confidence()
        missing = extraction.missing_critical_fields()

        if min_conf < CONFIDENCE_THRESHOLD or missing:
            logger.warning(
                json.dumps({
                    "severity": "WARNING",
                    "message": "Low confidence extraction — routing to human review",
                    "case_number": case_number,
                    "min_confidence": min_conf,
                    "missing_critical_fields": missing,
                    "threshold": CONFIDENCE_THRESHOLD,
                })
            )
            _write_to_firestore(case_number, extraction, document_uri, raw_msg)
            low_conf_payload = {
                **raw_msg,
                **extraction.to_firestore_dict(),
                "low_confidence_reason": {
                    "min_confidence": min_conf,
                    "missing_fields": missing,
                    "threshold": CONFIDENCE_THRESHOLD,
                },
            }
            _publish_result(PUBSUB_TOPIC_LOW_CONF, low_conf_payload, case_number)
            return

        # -------------------------------------------------------------------
        # Write to Firestore and publish to structured topic
        # -------------------------------------------------------------------
        _write_to_firestore(case_number, extraction, document_uri, raw_msg)

        structured_payload = {
            **raw_msg,
            **extraction.to_firestore_dict(),
            "min_extraction_confidence": min_conf,
        }
        _publish_result(PUBSUB_TOPIC_STRUCTURED, structured_payload, case_number)

        logger.info(
            json.dumps({
                "severity": "INFO",
                "message": "Filing processing complete",
                "case_number": case_number,
                "min_confidence": min_conf,
                "defenses_pre_screened": False,
            })
        )

    except Exception as exc:
        logger.error(
            json.dumps({
                "severity": "ERROR",
                "message": "Unhandled error in document processing",
                "case_number": case_number,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
        )
        # Re-raise so Cloud Functions retries the message (up to ack deadline)
        raise
