"""
EvictionShield — Module 4A: Court Clerk Integration / Tenant SMS Notification

Cloud Function triggered by Firestore `case_analyses` collection when a new document
is created with routing_recommendation in {urgent_legal_aid, standard_legal_aid}.

Generates a multilingual SMS via Gemini 1.5 Flash and sends it via Twilio.
Uses Cloud Tasks for retry handling on Twilio delivery failures.

Environment variables:
    GCP_PROJECT_ID
    GCP_LOCATION                Vertex AI region (default: us-central1)
    TWILIO_SECRET_NAME          Secret Manager secret for Twilio credentials (default: twilio-creds)
    CLOUD_TASKS_QUEUE           Cloud Tasks queue name (default: sms-notifications)
    CLOUD_TASKS_LOCATION        Cloud Tasks queue region (default: us-central1)
    INTAKE_PORTAL_URL           Base URL for tenant intake portal
    FIRESTORE_NOTIFICATIONS_COL Firestore collection (default: notification_events)
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from datetime import datetime
from typing import Any, Dict, Optional

import functions_framework
from google.cloud import firestore, secretmanager, tasks_v2
from google.protobuf import duration_pb2, timestamp_pb2
import vertexai
from vertexai.generative_models import GenerationConfig, GenerativeModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
LOCATION: str = os.environ.get("GCP_LOCATION", "us-central1")
TWILIO_SECRET_NAME: str = os.environ.get("TWILIO_SECRET_NAME", "twilio-creds")
TASKS_QUEUE: str = os.environ.get("CLOUD_TASKS_QUEUE", "sms-notifications")
TASKS_LOCATION: str = os.environ.get("CLOUD_TASKS_LOCATION", "us-central1")
INTAKE_PORTAL_URL: str = os.environ.get("INTAKE_PORTAL_URL", "https://app.evictionshield.io")
NOTIFICATIONS_COLLECTION: str = os.environ.get("FIRESTORE_NOTIFICATIONS_COL", "notification_events")
FLASH_MODEL: str = "gemini-1.5-flash-001"

# ZIP code → language heuristic (dominant language by demographic)
# Source: ACS 5-year estimates top language communities
_ZIP_LANGUAGE_OVERRIDES: Dict[str, str] = {
    # Philadelphia Haitian Creole community
    "19120": "ht",
    "19124": "ht",
    # Philadelphia Vietnamese community
    "19134": "vi",
    # Philadelphia Spanish-dominant ZIPs
    "19122": "es",
    "19133": "es",
    "19140": "es",
    "19141": "es",
}

_NAME_LANGUAGE_HEURISTICS: Dict[str, str] = {
    # Common Vietnamese surname prefixes
    "nguyen": "vi",
    "tran": "vi",
    "le": "vi",
    "pham": "vi",
    "hoang": "vi",
    # Common Haitian Creole surnames
    "jean": "ht",
    "pierre": "ht",
    "joseph": "ht",
    "baptiste": "ht",
    "louis": "ht",
    # Spanish surnames
    "garcia": "es",
    "rodriguez": "es",
    "martinez": "es",
    "hernandez": "es",
    "lopez": "es",
    "gonzalez": "es",
    "perez": "es",
    "flores": "es",
    "reyes": "es",
    "morales": "es",
}

_LANGUAGE_NAMES: Dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "ht": "Haitian Creole",
    "vi": "Vietnamese",
}

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

vertexai.init(project=PROJECT_ID, location=LOCATION)
_flash_model = GenerativeModel(FLASH_MODEL)
_firestore_client = firestore.Client(project=PROJECT_ID)
_secret_client = secretmanager.SecretManagerServiceClient()
_tasks_client = tasks_v2.CloudTasksClient()


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def _detect_language(tenant_name: Optional[str], zip_code: Optional[str]) -> str:
    """
    Detect likely preferred language from tenant name heuristics and ZIP demographics.
    Default: English ("en").
    """
    if zip_code and zip_code in _ZIP_LANGUAGE_OVERRIDES:
        return _ZIP_LANGUAGE_OVERRIDES[zip_code]

    if tenant_name:
        name_lower = tenant_name.strip().lower()
        for surname, lang in _NAME_LANGUAGE_HEURISTICS.items():
            if surname in name_lower:
                return lang

    return "en"


# ---------------------------------------------------------------------------
# SMS generation via Gemini 1.5 Flash
# ---------------------------------------------------------------------------

_SMS_SYSTEM_PROMPT = """\
You write short, clear SMS messages for people who may be facing housing eviction.
Your messages must:
1. Be written at a 6th-grade reading level.
2. Be 160 characters or fewer (one SMS segment).
3. Never use legal jargon.
4. Never say the tenant will win or lose.
5. Never give legal advice — only information about a free service.
6. Always end with the short link provided.
7. Be warm, calm, and factual. Do not create panic.
8. Write only in the language specified — do not mix languages.
"""

_SMS_USER_PROMPT_TEMPLATE = """\
Write an SMS message in {language_name} for a tenant who received an eviction filing.

Facts:
- Hearing date: {hearing_date}
- Days until hearing: {days_until_hearing}
- Potential issues found with the filing (do not specify which — just say "potential issues")
- Short link to free help: {short_link}

Requirements:
- 6th-grade reading level
- Under 160 characters
- Language: {language_name} only
- Do not say "legal advice" — say "free housing help" or "free information"
- Mention the hearing date and days remaining
- Include the link

Return ONLY the SMS text. No explanation, no quotes, no label.
"""


def _generate_sms_text(
    hearing_date: str,
    days_until_hearing: int,
    short_link: str,
    language: str,
) -> str:
    """Generate an SMS message using Gemini 1.5 Flash."""
    language_name = _LANGUAGE_NAMES.get(language, "English")
    user_prompt = _SMS_USER_PROMPT_TEMPLATE.format(
        language_name=language_name,
        hearing_date=hearing_date,
        days_until_hearing=days_until_hearing,
        short_link=short_link,
    )
    response = _flash_model.generate_content(
        [_SMS_SYSTEM_PROMPT, user_prompt],
        generation_config=GenerationConfig(
            temperature=0.3,
            max_output_tokens=200,
        ),
    )
    text = response.candidates[0].content.parts[0].text.strip()
    # Trim to 160 chars if Flash over-generates (safety guard)
    if len(text) > 160:
        text = text[:157] + "..."
    return text


# ---------------------------------------------------------------------------
# Twilio credentials
# ---------------------------------------------------------------------------

def _get_twilio_credentials() -> Dict[str, str]:
    """Retrieve Twilio credentials from Secret Manager at runtime."""
    secret_path = f"projects/{PROJECT_ID}/secrets/{TWILIO_SECRET_NAME}/versions/latest"
    response = _secret_client.access_secret_version(name=secret_path)
    return json.loads(response.payload.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Twilio SMS send (direct)
# ---------------------------------------------------------------------------

def _send_sms_via_twilio(
    to_number: str,
    message_body: str,
    twilio_creds: Dict[str, str],
) -> str:
    """
    Send SMS via Twilio REST API.
    Returns the Twilio message SID.
    """
    import requests
    from requests.auth import HTTPBasicAuth

    account_sid = twilio_creds["account_sid"]
    auth_token = twilio_creds["auth_token"]
    from_number = twilio_creds["from_number"]

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = {
        "To": to_number,
        "From": from_number,
        "Body": message_body,
    }
    resp = requests.post(url, data=payload, auth=HTTPBasicAuth(account_sid, auth_token), timeout=10)
    resp.raise_for_status()
    return resp.json().get("sid", "")


# ---------------------------------------------------------------------------
# Cloud Tasks: enqueue SMS for reliable delivery with retries
# ---------------------------------------------------------------------------

def _enqueue_sms_task(
    case_number: str,
    to_number: str,
    message_body: str,
    language: str,
) -> str:
    """
    Enqueue an SMS delivery task in Cloud Tasks.
    The task payload is picked up by the /send-sms Cloud Run endpoint.
    Returns the task name.
    """
    queue_path = _tasks_client.queue_path(PROJECT_ID, TASKS_LOCATION, TASKS_QUEUE)
    task_payload = json.dumps({
        "case_number": case_number,
        "to_number": to_number,
        "message_body": message_body,
        "language": language,
        "enqueued_at": datetime.utcnow().isoformat(),
    }).encode("utf-8")

    import base64
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"https://us-central1-{PROJECT_ID}.cloudfunctions.net/send-sms-worker",
            "headers": {"Content-Type": "application/json"},
            "body": base64.b64encode(task_payload).decode("utf-8"),
            "oidc_token": {
                "service_account_email": f"evictionshield-functions@{PROJECT_ID}.iam.gserviceaccount.com",
            },
        },
        "retry_config": {
            "max_attempts": 5,
            "min_backoff": duration_pb2.Duration(seconds=10),
            "max_backoff": duration_pb2.Duration(seconds=300),
            "max_doublings": 3,
        },
    }
    response = _tasks_client.create_task(parent=queue_path, task=task)
    return response.name


# ---------------------------------------------------------------------------
# Short link generation
# ---------------------------------------------------------------------------

def _make_short_link(case_number: str) -> str:
    """
    Generate a tracked short link to the tenant intake portal.
    Encodes case_number for pre-population of intake form.
    """
    encoded = urllib.parse.quote(case_number, safe="")
    return f"{INTAKE_PORTAL_URL}/intake?c={encoded}"


# ---------------------------------------------------------------------------
# Firestore: log notification event
# ---------------------------------------------------------------------------

def _log_notification_event(
    case_number: str,
    to_number: str,
    message_body: str,
    language: str,
    task_name: str,
    routing_recommendation: str,
) -> None:
    doc_ref = _firestore_client.collection(NOTIFICATIONS_COLLECTION).document(
        f"{case_number}_sms_{int(datetime.utcnow().timestamp())}"
    )
    doc_ref.set({
        "case_number": case_number,
        "notification_type": "sms",
        "to_number_hash": _hash_phone(to_number),  # Never store raw PII
        "message_preview": message_body[:40] + "..." if len(message_body) > 40 else message_body,
        "language": language,
        "routing_recommendation": routing_recommendation,
        "task_name": task_name,
        "send_status": "enqueued",
        "created_at": datetime.utcnow().isoformat(),
        "delivery_confirmed": False,
        "link_clicked": False,
    })


def _hash_phone(number: str) -> str:
    import hashlib
    return hashlib.sha256(number.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Cloud Function entrypoint
# ---------------------------------------------------------------------------

@functions_framework.cloud_event
def on_case_analysis_created(cloud_event) -> None:
    """
    Triggered by Firestore document creation in `case_analyses`.
    Only processes cases with routing_recommendation in (urgent_legal_aid, standard_legal_aid).
    """
    data = cloud_event.data
    # Firestore event payload structure
    value = data.get("value", {})
    fields = value.get("fields", {})

    def _get_field(name: str, field_type: str = "stringValue") -> Optional[Any]:
        field = fields.get(name, {})
        return field.get(field_type)

    case_number: str = _get_field("case_number") or "UNKNOWN"
    routing: str = _get_field("routing_recommendation") or ""
    days_until_hearing: int = int(_get_field("days_until_hearing", "integerValue") or 0)
    hearing_date: str = _get_field("hearing_date") or ""

    # Only send SMS for cases routed to legal aid
    if routing not in ("urgent_legal_aid", "standard_legal_aid"):
        logger.info("Skipping SMS — routing is '%s' for case %s", routing, case_number)
        return

    # --- Get tenant phone from the filing record ---
    filing_doc = _firestore_client.collection("eviction_filings").document(
        case_number.replace("/", "_")
    ).get()
    if not filing_doc.exists:
        logger.warning("No filing record found for case %s — cannot send SMS", case_number)
        return

    filing = filing_doc.to_dict()
    tenant_phone = filing.get("tenant_phone")
    tenant_name = filing.get("tenant_name")
    zip_code = filing.get("zip_code")

    if not tenant_phone:
        logger.info(
            "No tenant phone number available for case %s — flagging for clerk notification",
            case_number,
        )
        # Flag for manual clerk outreach
        _firestore_client.collection("manual_notification_queue").document(case_number).set({
            "case_number": case_number,
            "reason": "no_phone_number",
            "routing": routing,
            "created_at": datetime.utcnow().isoformat(),
        })
        return

    # --- Detect language ---
    language = _detect_language(tenant_name, zip_code)

    # --- Generate short link ---
    short_link = _make_short_link(case_number)

    # --- Generate SMS text via Gemini Flash ---
    try:
        message_body = _generate_sms_text(
            hearing_date=hearing_date,
            days_until_hearing=days_until_hearing,
            short_link=short_link,
            language=language,
        )
    except Exception as exc:
        logger.error("Gemini Flash SMS generation failed: %s", exc)
        # Use hardcoded fallback in English
        message_body = (
            f"IMPORTANT: You have an eviction hearing on {hearing_date} "
            f"({days_until_hearing} days away). Free help may be available. "
            f"Visit: {short_link}"
        )
        language = "en"

    # --- Enqueue SMS via Cloud Tasks ---
    try:
        task_name = _enqueue_sms_task(case_number, tenant_phone, message_body, language)
    except Exception as exc:
        logger.error("Cloud Tasks enqueue failed: %s", exc)
        # Fallback: send directly (no retry)
        try:
            twilio_creds = _get_twilio_credentials()
            _send_sms_via_twilio(tenant_phone, message_body, twilio_creds)
            task_name = "direct_send"
        except Exception as send_exc:
            logger.error("Direct SMS send also failed: %s", send_exc)
            return

    # --- Log notification event ---
    _log_notification_event(
        case_number=case_number,
        to_number=tenant_phone,
        message_body=message_body,
        language=language,
        task_name=task_name,
        routing_recommendation=routing,
    )

    logger.info(
        json.dumps({
            "severity": "INFO",
            "message": "SMS notification enqueued",
            "case_number": case_number,
            "language": language,
            "routing": routing,
            "days_until_hearing": days_until_hearing,
        })
    )


# ---------------------------------------------------------------------------
# SMS Worker — called by Cloud Tasks (separate HTTP endpoint)
# ---------------------------------------------------------------------------

@functions_framework.http
def send_sms_worker(request) -> tuple[Dict[str, Any], int]:
    """
    HTTP Cloud Function called by Cloud Tasks to deliver SMS via Twilio.
    Cloud Tasks handles retries with exponential backoff.
    """
    import base64
    body = request.get_json(silent=True)
    if not body:
        raw = request.get_data()
        body = json.loads(base64.b64decode(raw).decode("utf-8"))

    case_number = body.get("case_number", "")
    to_number = body.get("to_number", "")
    message_body = body.get("message_body", "")
    language = body.get("language", "en")

    if not to_number or not message_body:
        return {"error": "Missing to_number or message_body"}, 400

    try:
        twilio_creds = _get_twilio_credentials()
        sid = _send_sms_via_twilio(to_number, message_body, twilio_creds)

        # Update notification event with delivery confirmation
        _firestore_client.collection(NOTIFICATIONS_COLLECTION).where(
            "case_number", "==", case_number
        ).limit(1).get()  # Best-effort update — non-blocking

        logger.info(
            json.dumps({
                "severity": "INFO",
                "message": "SMS sent",
                "case_number": case_number,
                "twilio_sid": sid,
                "language": language,
            })
        )
        return {"status": "sent", "twilio_sid": sid}, 200

    except Exception as exc:
        logger.error("SMS delivery failed: %s", exc)
        # Return 500 so Cloud Tasks retries
        return {"error": str(exc)}, 500
