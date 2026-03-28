"""EvictionShield — Module 5A: Analytics Router"""
from __future__ import annotations
import os
from typing import Any, Dict

from fastapi import APIRouter, Depends
from google.cloud import bigquery, firestore

from ..auth import VerifiedUser, get_verified_user

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
BQ_DATASET = os.environ.get("BIGQUERY_DATASET", "evictionshield")

_bq = bigquery.Client(project=PROJECT_ID)
_db = firestore.Client(project=PROJECT_ID)
router = APIRouter()


@router.get("/summary")
async def get_summary(user: VerifiedUser = Depends(get_verified_user)) -> Dict[str, Any]:
    """
    Returns organization-level metrics:
      - cases received, resolved, evictions prevented
      - attorney utilization rate
      - average days from filing to assignment
    """
    org_id = user.org_id
    zip_filter = user.coverage_zip_codes[:30] if user.coverage_zip_codes else []

    # Firestore-side counts
    analyses_ref = _db.collection("case_analyses")
    if zip_filter:
        analyses_ref = analyses_ref.where("zip_code", "in", zip_filter)

    all_cases = list(analyses_ref.select(
        ["routing_recommendation", "overall_case_strength", "assigned_attorney_uid",
         "assignment_timestamp", "ingestion_timestamp"]
    ).stream())

    total_received = len(all_cases)
    assigned = sum(1 for c in all_cases if c.to_dict().get("assigned_attorney_uid"))
    urgent = sum(1 for c in all_cases if c.to_dict().get("routing_recommendation") == "urgent_legal_aid")

    # Outcome counts from BigQuery
    outcome_query = f"""
        SELECT
            outcome,
            COUNT(*) AS count
        FROM `{PROJECT_ID}.{BQ_DATASET}.filing_events`
        WHERE jurisdiction LIKE @state_prefix
          AND outcome IS NOT NULL
        GROUP BY outcome
    """
    state_prefix = f"{user.coverage_states[0]}%" if user.coverage_states else "PA%"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("state_prefix", "STRING", state_prefix)]
    )
    try:
        outcome_rows = list(_bq.query(outcome_query, job_config=job_config).result())
        outcomes = {r["outcome"]: r["count"] for r in outcome_rows}
    except Exception:
        outcomes = {}

    evictions_prevented = outcomes.get("won", 0) + outcomes.get("dismissed", 0) + outcomes.get("settled", 0)

    # Avg days to assignment
    avg_days_to_assign = None
    assignment_gaps = []
    for c in all_cases:
        data = c.to_dict()
        if data.get("assignment_timestamp") and data.get("ingestion_timestamp"):
            try:
                from datetime import datetime
                t1 = datetime.fromisoformat(str(data["ingestion_timestamp"]))
                t2 = datetime.fromisoformat(str(data["assignment_timestamp"]))
                gap = (t2 - t1).total_seconds() / 3600  # hours
                assignment_gaps.append(gap)
            except Exception:
                pass
    if assignment_gaps:
        avg_days_to_assign = round(sum(assignment_gaps) / len(assignment_gaps), 1)

    # Attorney utilization: unique attorneys with active cases / total attorneys in org
    active_attorneys = len(set(
        c.to_dict().get("assigned_attorney_uid") for c in all_cases
        if c.to_dict().get("assigned_attorney_uid")
    ))
    org_attorneys = len(list(
        _db.collection("user_profiles")
        .where("org_id", "==", org_id)
        .where("role", "in", ["attorney", "admin"])
        .select([]).stream()
    ))
    utilization_rate = round(active_attorneys / org_attorneys * 100, 1) if org_attorneys else 0.0

    return {
        "org_id": org_id,
        "org_name": user.org_name,
        "cases_received": total_received,
        "cases_assigned": assigned,
        "cases_urgent": urgent,
        "evictions_prevented": evictions_prevented,
        "outcome_breakdown": outcomes,
        "attorney_utilization_rate_pct": utilization_rate,
        "active_attorneys": active_attorneys,
        "total_attorneys": org_attorneys,
        "avg_hours_to_assignment": avg_days_to_assign,
    }
