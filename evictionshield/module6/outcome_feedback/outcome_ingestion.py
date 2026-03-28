"""
EvictionShield — Module 6A: Outcome Ingestion Cloud Function

Triggered by new documents in Firestore `case_outcomes` collection.
1. Updates BigQuery filing_events with outcome data
2. Triggers landlord_profiles recalculation
3. Updates defense_effectiveness aggregate statistics
4. Creates analysis_reviews for high-confidence defense losses
5. Triggers Dataflow pipeline for landlord profile refresh

Environment variables:
    GCP_PROJECT_ID
    BIGQUERY_DATASET          (default: evictionshield)
    FIRESTORE_REVIEWS_COL     (default: analysis_reviews)
    PUBSUB_TOPIC_PROFILE_UPDATE (default: trigger-profile-update)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import functions_framework
from google.cloud import bigquery, firestore, pubsub_v1

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
BQ_DATASET: str = os.environ.get("BIGQUERY_DATASET", "evictionshield")
REVIEWS_COLLECTION: str = os.environ.get("FIRESTORE_REVIEWS_COL", "analysis_reviews")
PUBSUB_TOPIC_PROFILE_UPDATE: str = os.environ.get("PUBSUB_TOPIC_PROFILE_UPDATE", "trigger-profile-update")

_db = firestore.Client(project=PROJECT_ID)
_bq = bigquery.Client(project=PROJECT_ID)
_publisher = pubsub_v1.PublisherClient()


# ---------------------------------------------------------------------------
# BigQuery outcome update
# ---------------------------------------------------------------------------

def _update_filing_event_outcome(outcome_data: Dict[str, Any]) -> None:
    """
    Update the filing_events BigQuery table with post-hearing outcome.
    Uses MERGE statement for idempotency.
    """
    case_number = outcome_data.get("case_number", "")
    outcome = outcome_data.get("outcome", "")
    tenant_was_represented = outcome_data.get("tenant_was_represented", False)
    outcome_date = outcome_data.get("recorded_at", datetime.utcnow().isoformat())[:10]

    merge_sql = f"""
        MERGE `{PROJECT_ID}.{BQ_DATASET}.filing_events` T
        USING (
            SELECT
                @case_number AS case_number,
                @outcome AS outcome,
                DATE(@outcome_date) AS outcome_date,
                @represented AS represented,
                CASE WHEN @outcome IN ('won', 'dismissed') THEN FALSE ELSE T.withdrawn END AS withdrawn
        ) S
        ON T.case_number = S.case_number
        WHEN MATCHED THEN
            UPDATE SET
                T.outcome = S.outcome,
                T.outcome_date = S.outcome_date,
                T.represented = S.represented
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("case_number", "STRING", case_number),
            bigquery.ScalarQueryParameter("outcome", "STRING", outcome),
            bigquery.ScalarQueryParameter("outcome_date", "STRING", outcome_date),
            bigquery.ScalarQueryParameter("represented", "BOOL", tenant_was_represented),
        ]
    )
    try:
        _bq.query(merge_sql, job_config=job_config).result()
        logger.info("Updated BigQuery filing_events for case %s: outcome=%s", case_number, outcome)
    except Exception as exc:
        logger.error("BigQuery outcome update failed for %s: %s", case_number, exc)
        raise


# ---------------------------------------------------------------------------
# Defense effectiveness update
# ---------------------------------------------------------------------------

def _update_defense_effectiveness(
    case_number: str,
    jurisdiction: str,
    defenses_raised: List[str],
    defenses_succeeded: List[str],
    tenant_was_represented: bool,
    landlord_entity_type: str,
) -> None:
    """
    Update the defense_effectiveness aggregate table.
    Increments counters for each raised defense.
    Uses MERGE for idempotency (keyed on defense_type + jurisdiction + tenant_represented).
    """
    if not defenses_raised:
        return

    for defense_type in defenses_raised:
        succeeded = defense_type in defenses_succeeded
        merge_sql = f"""
            MERGE `{PROJECT_ID}.{BQ_DATASET}.defense_effectiveness` T
            USING (
                SELECT
                    @defense_type AS defense_type,
                    @jurisdiction AS jurisdiction,
                    @tenant_represented AS tenant_represented,
                    @landlord_entity_type AS landlord_entity_type
            ) S
            ON T.defense_type = S.defense_type
               AND T.jurisdiction = S.jurisdiction
               AND T.tenant_represented = S.tenant_represented
               AND T.landlord_entity_type = S.landlord_entity_type
            WHEN MATCHED THEN
                UPDATE SET
                    T.total_cases = T.total_cases + 1,
                    T.cases_defense_succeeded = T.cases_defense_succeeded + @succeeded_int,
                    T.defense_success_rate = SAFE_DIVIDE(
                        T.cases_defense_succeeded + @succeeded_int,
                        T.total_cases + 1
                    ),
                    T.sample_size = T.total_cases + 1,
                    T.last_updated = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN
                INSERT (
                    defense_type, jurisdiction, tenant_represented, landlord_entity_type,
                    total_cases, cases_defense_succeeded, defense_success_rate, sample_size, last_updated
                )
                VALUES (
                    S.defense_type, S.jurisdiction, S.tenant_represented, S.landlord_entity_type,
                    1, @succeeded_int, @succeeded_float, 1, CURRENT_TIMESTAMP()
                )
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("defense_type", "STRING", defense_type),
                bigquery.ScalarQueryParameter("jurisdiction", "STRING", jurisdiction),
                bigquery.ScalarQueryParameter("tenant_represented", "BOOL", tenant_was_represented),
                bigquery.ScalarQueryParameter("landlord_entity_type", "STRING", landlord_entity_type),
                bigquery.ScalarQueryParameter("succeeded_int", "INT64", 1 if succeeded else 0),
                bigquery.ScalarQueryParameter("succeeded_float", "FLOAT64", 1.0 if succeeded else 0.0),
            ]
        )
        try:
            _bq.query(merge_sql, job_config=job_config).result()
        except Exception as exc:
            logger.error(
                "Defense effectiveness update failed for %s/%s: %s",
                defense_type, jurisdiction, exc,
            )


# ---------------------------------------------------------------------------
# Analysis review creation
# ---------------------------------------------------------------------------

def _create_analysis_review_if_needed(
    case_number: str,
    outcome: str,
    defenses_raised: List[str],
    defenses_identified_with_confidence: List[Dict[str, Any]],
) -> None:
    """
    If a high-confidence defense was raised but the tenant lost, create a review
    record for the legal team to analyze why the system's confidence was wrong.
    """
    if outcome not in ("lost",):
        return

    high_conf_raised = [
        d for d in defenses_identified_with_confidence
        if d.get("confidence") == "high" and d.get("defense_type") in defenses_raised
    ]

    if not high_conf_raised:
        return

    review_data = {
        "case_number": case_number,
        "review_type": "high_confidence_defense_failed",
        "outcome": outcome,
        "defenses_under_review": [
            {
                "defense_type": d.get("defense_type"),
                "confidence": d.get("confidence"),
                "legal_basis": d.get("legal_basis"),
                "evidence_in_filing": d.get("evidence_in_filing"),
            }
            for d in high_conf_raised
        ],
        "created_at": datetime.utcnow().isoformat(),
        "review_status": "pending",
        "review_notes": None,
    }

    _db.collection(REVIEWS_COLLECTION).document(
        f"{case_number}_{int(datetime.utcnow().timestamp())}"
    ).set(review_data)

    logger.warning(
        "Created analysis review: %d high-confidence defense(s) failed in case %s",
        len(high_conf_raised), case_number,
    )


# ---------------------------------------------------------------------------
# Trigger landlord profile refresh
# ---------------------------------------------------------------------------

def _trigger_landlord_profile_refresh(landlord_id: str, case_number: str) -> None:
    """
    Publish a message to trigger incremental landlord profile recalculation.
    The Dataflow pipeline or a lightweight Cloud Function picks this up.
    """
    topic_path = f"projects/{PROJECT_ID}/topics/{PUBSUB_TOPIC_PROFILE_UPDATE}"
    payload = json.dumps({
        "landlord_id": landlord_id,
        "trigger_reason": "outcome_recorded",
        "case_number": case_number,
        "timestamp": datetime.utcnow().isoformat(),
    }).encode("utf-8")
    try:
        future = _publisher.publish(topic_path, data=payload, landlord_id=landlord_id)
        future.result(timeout=10)
        logger.info("Profile refresh triggered for landlord %s", landlord_id)
    except Exception as exc:
        logger.warning("Failed to trigger profile refresh: %s", exc)


# ---------------------------------------------------------------------------
# Cloud Function entrypoint
# ---------------------------------------------------------------------------

@functions_framework.cloud_event
def on_case_outcome_created(cloud_event) -> None:
    """
    Triggered by new Firestore document in `case_outcomes`.
    """
    data = cloud_event.data
    value = data.get("value", {})
    fields = value.get("fields", {})

    def _str(name: str) -> str:
        return fields.get(name, {}).get("stringValue", "")

    def _bool(name: str) -> bool:
        return fields.get(name, {}).get("booleanValue", False)

    def _list(name: str) -> List[str]:
        arr = fields.get(name, {}).get("arrayValue", {})
        return [v.get("stringValue", "") for v in arr.get("values", [])]

    case_number = _str("case_number")
    outcome = _str("outcome")

    if not case_number or not outcome:
        logger.warning("Incomplete outcome event — missing case_number or outcome")
        return

    logger.info("Processing outcome event: case=%s outcome=%s", case_number, outcome)

    outcome_data = {
        "case_number": case_number,
        "outcome": outcome,
        "tenant_was_represented": _bool("tenant_was_represented"),
        "defenses_raised": _list("defenses_raised"),
        "defenses_that_succeeded": _list("defenses_that_succeeded"),
        "recorded_at": _str("recorded_at") or datetime.utcnow().isoformat(),
    }

    # Load case analysis for supplemental data
    doc_id = case_number.replace("/", "_")
    analysis_doc = _db.collection("case_analyses").document(doc_id).get()
    analysis_data = analysis_doc.to_dict() if analysis_doc.exists else {}

    jurisdiction = analysis_data.get("jurisdiction", "PA-UJS")
    landlord_profile = analysis_data.get("landlord_profile", {})
    landlord_id = analysis_data.get("landlord_id", "")
    landlord_entity_type = landlord_profile.get("entity_type", "unknown")
    defenses_identified = analysis_data.get("defenses_identified", [])
    if isinstance(defenses_identified, str):
        try:
            defenses_identified = json.loads(defenses_identified)
        except (json.JSONDecodeError, TypeError):
            defenses_identified = []

    # 1. Update BigQuery filing_events
    try:
        _update_filing_event_outcome(outcome_data)
    except Exception as exc:
        logger.error("filing_events update failed: %s", exc)

    # 2. Update defense effectiveness
    try:
        _update_defense_effectiveness(
            case_number=case_number,
            jurisdiction=jurisdiction,
            defenses_raised=outcome_data["defenses_raised"],
            defenses_succeeded=outcome_data["defenses_that_succeeded"],
            tenant_was_represented=outcome_data["tenant_was_represented"],
            landlord_entity_type=landlord_entity_type,
        )
    except Exception as exc:
        logger.error("Defense effectiveness update failed: %s", exc)

    # 3. Create review record if needed
    try:
        _create_analysis_review_if_needed(
            case_number=case_number,
            outcome=outcome,
            defenses_raised=outcome_data["defenses_raised"],
            defenses_identified_with_confidence=defenses_identified,
        )
    except Exception as exc:
        logger.error("Analysis review creation failed: %s", exc)

    # 4. Trigger landlord profile refresh
    if landlord_id:
        _trigger_landlord_profile_refresh(landlord_id, case_number)

    logger.info(
        json.dumps({
            "severity": "INFO",
            "message": "Outcome ingestion complete",
            "case_number": case_number,
            "outcome": outcome,
            "defenses_raised_count": len(outcome_data["defenses_raised"]),
            "defenses_succeeded_count": len(outcome_data["defenses_that_succeeded"]),
        })
    )
