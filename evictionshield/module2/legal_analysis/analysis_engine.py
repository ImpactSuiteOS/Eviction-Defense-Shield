"""
EvictionShield — Module 2C: Gemini Legal Analysis Engine

Accepts a structured_filing dict from Firestore, retrieves landlord history from BigQuery,
calls Gemini 1.5 Pro via Vertex AI, validates the response, and writes to Firestore.

Environment variables:
    GCP_PROJECT_ID
    GCP_LOCATION                   Vertex AI region (default: us-central1)
    GEMINI_MODEL                   Model ID (default: gemini-1.5-pro-001)
    GCS_BUCKET_RULESETS            Cloud Storage bucket for jurisdiction YAML files
    FIRESTORE_ANALYSIS_COLLECTION  (default: case_analyses)
    BIGQUERY_DATASET               Dataset for landlord profiles (default: evictionshield)
    PUBSUB_TOPIC_ANALYSIS_DONE     (default: case-analysis-complete)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import functions_framework
import base64
from google.cloud import bigquery, firestore, pubsub_v1
from pydantic import ValidationError
import vertexai
from vertexai.generative_models import GenerationConfig, GenerativeModel

from .jurisdiction_ruleset import InsufficientRulesError, JurisdictionRuleset
from .schemas import (
    DefenseSuccessProbability,
    EvictionAnalysisResult,
    LandlordProfile,
    LEGAL_DISCLAIMER,
)
from .system_prompt import build_correction_prompt, build_system_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
LOCATION: str = os.environ.get("GCP_LOCATION", "us-central1")
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-1.5-pro-001")
GCS_BUCKET_RULESETS: str = os.environ.get("GCS_BUCKET_RULESETS", "evictionshield-rulesets")
FIRESTORE_ANALYSIS_COLLECTION: str = os.environ.get("FIRESTORE_ANALYSIS_COLLECTION", "case_analyses")
BQ_DATASET: str = os.environ.get("BIGQUERY_DATASET", "evictionshield")
PUBSUB_TOPIC_ANALYSIS_DONE: str = os.environ.get("PUBSUB_TOPIC_ANALYSIS_DONE", "case-analysis-complete")

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

vertexai.init(project=PROJECT_ID, location=LOCATION)
_model = GenerativeModel(GEMINI_MODEL)
_firestore_client = firestore.Client(project=PROJECT_ID)
_bq_client = bigquery.Client(project=PROJECT_ID)
_pubsub_publisher = pubsub_v1.PublisherClient()
_ruleset_loader = JurisdictionRuleset(project_id=PROJECT_ID, gcs_bucket=GCS_BUCKET_RULESETS)


# ---------------------------------------------------------------------------
# BigQuery: landlord profile retrieval
# ---------------------------------------------------------------------------

def _get_landlord_profile_from_bq(landlord_name: str, state: str) -> Dict[str, Any]:
    """
    Retrieve landlord behavioral profile from BigQuery landlord_profiles table.
    Uses deterministic ID: SHA-256 of normalized_name + state.
    Falls back to zero-filled profile if landlord not found (new filer).
    """
    import hashlib

    normalized = landlord_name.strip().upper()
    landlord_id = hashlib.sha256(f"{normalized}|{state.upper()}".encode()).hexdigest()[:32]

    query = f"""
        SELECT
            landlord_name_normalized,
            entity_type,
            filings_last_90_days,
            filings_last_365_days,
            withdrawal_rate_pct,
            habitability_complaint_count,
            complaint_to_filing_correlation,
            protected_class_filing_pct,
            procedural_defect_rate,
            CASE
                WHEN filings_last_90_days > 20 THEN 'high_volume_filer'
                WHEN withdrawal_rate_pct > 40.0 THEN 'high_withdrawal_rate'
                WHEN complaint_to_filing_correlation > 0.5 THEN 'retaliation_pattern'
                ELSE NULL
            END AS pattern_flag
        FROM `{PROJECT_ID}.{BQ_DATASET}.landlord_profiles`
        WHERE landlord_id = @landlord_id
        LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("landlord_id", "STRING", landlord_id)]
    )
    try:
        rows = list(_bq_client.query(query, job_config=job_config).result())
    except Exception as exc:
        logger.warning("BigQuery landlord lookup failed: %s — using empty profile", exc)
        rows = []

    if rows:
        row = dict(rows[0])
        return {
            "landlord_name": landlord_name,
            "entity_type": row.get("entity_type", "unknown"),
            "filing_count_90_days": int(row.get("filings_last_90_days", 0)),
            "withdrawal_rate_pct": float(row.get("withdrawal_rate_pct", 0.0)),
            "prior_habitability_complaints": int(row.get("habitability_complaint_count", 0)),
            "pattern_flag": row.get("pattern_flag"),
            "complaint_to_filing_correlation": float(row.get("complaint_to_filing_correlation", 0.0)),
            "protected_class_filing_pct": float(row.get("protected_class_filing_pct", 0.0)),
            "procedural_defect_rate": float(row.get("procedural_defect_rate", 0.0)),
        }
    else:
        logger.info("No BigQuery profile for landlord '%s' — using defaults", landlord_name)
        return {
            "landlord_name": landlord_name,
            "entity_type": "unknown",
            "filing_count_90_days": 0,
            "withdrawal_rate_pct": 0.0,
            "prior_habitability_complaints": 0,
            "pattern_flag": None,
            "complaint_to_filing_correlation": 0.0,
            "protected_class_filing_pct": 0.0,
            "procedural_defect_rate": 0.0,
        }


# ---------------------------------------------------------------------------
# BigQuery ML: defense success probabilities
# ---------------------------------------------------------------------------

def _get_defense_probabilities(
    jurisdiction: str, defense_types: List[str]
) -> List[DefenseSuccessProbability]:
    """
    Query the BigQuery ML model for predicted success probabilities.
    Returns empty list on any failure so analysis can proceed without ML data.
    """
    if not defense_types:
        return []

    defense_list = ", ".join(f"'{d}'" for d in defense_types)
    query = f"""
        SELECT
            defense_type,
            jurisdiction,
            ROUND(predicted_defense_succeeded_probs[OFFSET(1)].prob * 100, 1) AS success_probability_pct,
            sample_size
        FROM
            ML.PREDICT(
                MODEL `{PROJECT_ID}.{BQ_DATASET}.defense_effectiveness_model`,
                (
                    SELECT
                        defense_type,
                        jurisdiction,
                        TRUE AS tenant_represented,
                        sample_size
                    FROM `{PROJECT_ID}.{BQ_DATASET}.defense_effectiveness`
                    WHERE jurisdiction = @jurisdiction
                      AND defense_type IN ({defense_list})
                )
            )
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("jurisdiction", "STRING", jurisdiction)
        ]
    )
    try:
        rows = list(_bq_client.query(query, job_config=job_config).result())
        return [
            DefenseSuccessProbability(
                defense_type=row["defense_type"],
                jurisdiction=row["jurisdiction"],
                success_probability_pct=float(row["success_probability_pct"] or 0.0),
                sample_size=int(row["sample_size"] or 0),
                represented_only=True,
            )
            for row in rows
        ]
    except Exception as exc:
        logger.warning("BigQuery ML defense probability query failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_user_prompt(
    filing: Dict[str, Any],
    landlord_profile: Dict[str, Any],
    defense_probabilities: List[DefenseSuccessProbability],
    days_until_hearing: int,
) -> str:
    """
    Construct the user turn of the Gemini conversation.
    Combines structured filing data + landlord profile + BQML probabilities.
    """
    prob_section = ""
    if defense_probabilities:
        lines = []
        for dp in defense_probabilities:
            if dp.sample_size >= 20:  # Only surface statistically meaningful estimates
                lines.append(
                    f"  - {dp.defense_type}: {dp.success_probability_pct:.1f}% success rate "
                    f"when raised by represented tenants in {dp.jurisdiction} "
                    f"(based on {dp.sample_size} historical cases)"
                )
        if lines:
            prob_section = (
                "\n\nHISTORICAL DEFENSE SUCCESS RATES (from EvictionShield outcome database):\n"
                + "\n".join(lines)
                + "\nNote: Use these rates as context only. Individual case outcomes vary."
            )

    return f"""Analyze the following eviction filing and produce a complete JSON analysis.

FILING DATA:
{json.dumps(filing, indent=2, default=str)}

LANDLORD BEHAVIORAL PROFILE:
{json.dumps(landlord_profile, indent=2)}

DAYS UNTIL HEARING: {days_until_hearing}
{prob_section}

Return ONLY a valid JSON object matching the required schema. No prose, no markdown, no code fences.
"""


# ---------------------------------------------------------------------------
# Gemini call with validation and retry
# ---------------------------------------------------------------------------

def _call_gemini(system_prompt: str, user_prompt: str) -> str:
    """
    Call Gemini 1.5 Pro via Vertex AI.
    Returns the raw text response.
    Raises on quota exceeded, safety block, or network failure.
    """
    generation_config = GenerationConfig(
        temperature=0.1,          # Low temperature for deterministic legal analysis
        top_p=0.95,
        max_output_tokens=8192,
        response_mime_type="application/json",
    )
    response = _model.generate_content(
        [system_prompt, user_prompt],
        generation_config=generation_config,
    )
    # Check for safety blocks
    if not response.candidates:
        raise RuntimeError("Gemini returned no candidates — possible safety filter block")
    candidate = response.candidates[0]
    if candidate.finish_reason.name not in ("STOP", "MAX_TOKENS"):
        raise RuntimeError(f"Gemini finish reason: {candidate.finish_reason.name}")
    return candidate.content.parts[0].text


def _parse_and_validate(raw_json: str) -> EvictionAnalysisResult:
    """
    Parse raw Gemini JSON output and validate against Pydantic schema.
    Raises ValidationError on failure so caller can issue correction prompt.
    """
    # Strip any accidental markdown fences
    text = raw_json.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    data = json.loads(text)
    return EvictionAnalysisResult(**data)


# ---------------------------------------------------------------------------
# Firestore write (idempotent on case_number)
# ---------------------------------------------------------------------------

def _write_analysis_to_firestore(result: EvictionAnalysisResult) -> None:
    doc_id = result.case_number.replace("/", "_")
    doc_ref = _firestore_client.collection(FIRESTORE_ANALYSIS_COLLECTION).document(doc_id)
    data = result.to_firestore_dict()
    data["gemini_model"] = GEMINI_MODEL
    doc_ref.set(data, merge=True)
    logger.info("Analysis written to Firestore: %s", doc_id)


def _publish_analysis_complete(result: EvictionAnalysisResult) -> None:
    topic_path = f"projects/{PROJECT_ID}/topics/{PUBSUB_TOPIC_ANALYSIS_DONE}"
    payload = json.dumps({
        "case_number": result.case_number,
        "routing_recommendation": result.routing_recommendation,
        "overall_case_strength": result.overall_case_strength,
        "days_until_hearing": result.days_until_hearing,
        "defenses_count": len(result.defenses_identified),
        "jurisdiction": result.jurisdiction,
        "analysis_timestamp": result.analysis_timestamp.isoformat(),
    }).encode("utf-8")
    future = _pubsub_publisher.publish(
        topic_path,
        data=payload,
        case_number=result.case_number,
        routing=result.routing_recommendation,
    )
    future.result(timeout=30)


# ---------------------------------------------------------------------------
# Main analysis function
# ---------------------------------------------------------------------------

def analyze_filing(structured_filing: Dict[str, Any]) -> EvictionAnalysisResult:
    """
    Full analysis pipeline for one structured filing.

    1. Load jurisdiction ruleset
    2. Fetch landlord profile from BigQuery
    3. Compute days until hearing
    4. Get defense probabilities from BQML
    5. Build prompts
    6. Call Gemini with one validation retry
    7. Write to Firestore + publish

    Args:
        structured_filing: Dict from Firestore eviction_filings collection.

    Returns:
        Validated EvictionAnalysisResult instance.

    Raises:
        RuntimeError if Gemini fails after retry, Firestore write fails, or BQ quota exceeded.
    """
    case_number: str = structured_filing.get("case_number", "UNKNOWN")
    jurisdiction_code: str = structured_filing.get("jurisdiction_code", "PA-UJS")
    state: str = structured_filing.get("state", "PA")
    landlord_name: str = structured_filing.get("landlord_name", "Unknown Landlord")

    logger.info(
        json.dumps({
            "severity": "INFO",
            "message": "Starting Gemini analysis",
            "case_number": case_number,
            "jurisdiction": jurisdiction_code,
        })
    )

    # --- 1. Load jurisdiction ruleset ---
    try:
        _ruleset_loader.validate_ruleset(jurisdiction_code)
        jurisdiction_rules_section = _ruleset_loader.render_for_prompt(jurisdiction_code)
    except InsufficientRulesError as exc:
        logger.error(
            "Insufficient jurisdiction rules: %s — cannot perform reliable analysis", exc
        )
        jurisdiction_rules_section = (
            f"WARNING: Ruleset for {jurisdiction_code} is incomplete. "
            f"Missing: {exc}. Apply general principles and flag all defenses as LOW confidence."
        )

    # --- 2. Fetch landlord profile ---
    landlord_profile_dict = _get_landlord_profile_from_bq(landlord_name, state)

    # --- 3. Days until hearing ---
    hearing_date_raw = structured_filing.get("hearing_date")
    if hearing_date_raw:
        if isinstance(hearing_date_raw, str):
            hearing_date = date.fromisoformat(hearing_date_raw[:10])
        elif isinstance(hearing_date_raw, date):
            hearing_date = hearing_date_raw
        else:
            hearing_date = date.today()
    else:
        hearing_date = date.today()
    days_until_hearing = max(0, (hearing_date - date.today()).days)

    # --- 4. Build system prompt ---
    system_prompt = build_system_prompt(jurisdiction_rules_section)

    # --- 5. Get defense probabilities (pre-warm for prompt injection) ---
    # We'll get probabilities for all 12 known defense types as a pre-fetch
    all_defense_types = [
        "improper_notice_period", "defective_notice_delivery", "missing_required_disclosures",
        "retaliatory_eviction", "waiver_by_rent_acceptance", "habitability_defense",
        "landlord_licensure_deficiency", "discriminatory_filing_pattern",
        "procedural_defect_summons", "improper_plaintiff",
        "section8_procedural_violation", "moratorium_applicability",
    ]
    defense_probabilities = _get_defense_probabilities(jurisdiction_code, all_defense_types)

    # --- 6. Build user prompt ---
    user_prompt = _build_user_prompt(
        filing=structured_filing,
        landlord_profile=landlord_profile_dict,
        defense_probabilities=defense_probabilities,
        days_until_hearing=days_until_hearing,
    )

    # --- 7. Call Gemini with retry on schema validation failure ---
    t0 = time.monotonic()
    raw_response: str = ""
    result: Optional[EvictionAnalysisResult] = None
    last_error: Optional[Exception] = None

    for attempt in range(1, 3):  # Max 2 attempts (original + 1 correction)
        try:
            if attempt == 1:
                raw_response = _call_gemini(system_prompt, user_prompt)
            else:
                # Correction prompt: ask Gemini to fix the schema violation
                correction_prompt = build_correction_prompt(
                    validation_error=str(last_error),
                    previous_response=raw_response,
                )
                raw_response = _call_gemini(system_prompt, correction_prompt)

            result = _parse_and_validate(raw_response)
            break  # Success

        except (ValidationError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc
            logger.warning(
                "Gemini response validation failed (attempt %d/2): %s", attempt, exc
            )
            if attempt == 2:
                raise RuntimeError(
                    f"Gemini schema validation failed after 2 attempts for {case_number}. "
                    f"Last error: {exc}"
                ) from exc

        except Exception as exc:
            # Non-retryable: quota, network, safety block
            raise RuntimeError(
                f"Gemini API call failed for {case_number}: {exc}"
            ) from exc

    gemini_latency_ms = int((time.monotonic() - t0) * 1000)

    # Inject defense probabilities post-validation
    if result and defense_probabilities:
        identified_types = {d.defense_type for d in result.defenses_identified}
        result.defense_probabilities = [
            dp for dp in defense_probabilities if dp.defense_type in identified_types
        ]

    # --- 8. Write to Firestore ---
    try:
        _write_analysis_to_firestore(result)
    except Exception as exc:
        raise RuntimeError(
            f"Firestore write failed for case {case_number}: {exc}"
        ) from exc

    # --- 9. Publish completion event ---
    try:
        _publish_analysis_complete(result)
    except Exception as exc:
        logger.warning("Pub/Sub publish failed (non-fatal): %s", exc)

    # --- 10. Structured log ---
    logger.info(
        json.dumps({
            "severity": "INFO",
            "message": "Gemini analysis complete",
            "case_number": case_number,
            "defenses_found": len(result.defenses_identified),
            "overall_case_strength": result.overall_case_strength,
            "routing_recommendation": result.routing_recommendation,
            "gemini_latency_ms": gemini_latency_ms,
            "gemini_model": GEMINI_MODEL,
            "jurisdiction": jurisdiction_code,
            "days_until_hearing": days_until_hearing,
        })
    )

    return result


# ---------------------------------------------------------------------------
# Cloud Function entrypoint (triggered by structured-filings-ready Pub/Sub)
# ---------------------------------------------------------------------------

@functions_framework.cloud_event
def analyze_filing_event(cloud_event) -> None:
    """
    Cloud Function entrypoint.
    Triggered by messages on the `structured-filings-ready` Pub/Sub topic.
    """
    import base64
    pubsub_data = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
    filing_dict: Dict[str, Any] = json.loads(pubsub_data)

    case_number = filing_dict.get("case_number", "UNKNOWN")
    try:
        analyze_filing(filing_dict)
    except Exception as exc:
        logger.error(
            json.dumps({
                "severity": "ERROR",
                "message": "Analysis pipeline failure",
                "case_number": case_number,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
        )
        raise  # Re-raise for Cloud Functions retry
