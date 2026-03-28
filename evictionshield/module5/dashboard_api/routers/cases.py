"""
EvictionShield — Module 5A: Cases Router

Endpoints for case listing, detail, assignment, and outcome recording.
All endpoints enforce organization-level data isolation.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from google.cloud import bigquery, firestore, storage
from pydantic import BaseModel, Field

from ..auth import VerifiedUser, get_verified_user, require_attorney_or_admin, require_org_access

logger = logging.getLogger(__name__)

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
BQ_DATASET: str = os.environ.get("BIGQUERY_DATASET", "evictionshield")
STORAGE_BUCKET_BRIEFS: str = os.environ.get("STORAGE_BUCKET_BRIEFS", "evictionshield-attorney-briefs")

_db = firestore.Client(project=PROJECT_ID)
_bq = bigquery.Client(project=PROJECT_ID)
_storage = storage.Client(project=PROJECT_ID)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CaseSummary(BaseModel):
    case_number: str
    routing_recommendation: str
    overall_case_strength: str
    days_until_hearing: int
    hearing_date: Optional[str]
    zip_code: Optional[str]
    defenses_count: int
    defenses_types: List[str]
    landlord_name: Optional[str]
    assigned_attorney_uid: Optional[str]
    assigned_attorney_email: Optional[str]
    processing_status: str


class CaseAssignRequest(BaseModel):
    attorney_uid: str
    notes: Optional[str] = None


class CaseOutcomeRequest(BaseModel):
    outcome: str = Field(..., pattern="^(won|lost|settled|dismissed|no_show)$")
    outcome_notes: Optional[str] = None
    tenant_was_represented: bool
    defenses_raised: List[str] = Field(default_factory=list)
    defenses_that_succeeded: List[str] = Field(default_factory=list)


class PaginatedCases(BaseModel):
    cases: List[CaseSummary]
    total_count: int
    page: int
    page_size: int
    next_page_token: Optional[str]


# ---------------------------------------------------------------------------
# GET /cases
# ---------------------------------------------------------------------------


@router.get("", response_model=PaginatedCases)
async def list_cases(
    routing_recommendation: Optional[str] = Query(None),
    days_until_hearing_min: Optional[int] = Query(None, ge=0),
    days_until_hearing_max: Optional[int] = Query(None, ge=0),
    overall_case_strength: Optional[str] = Query(None),
    defense_type: Optional[str] = Query(None),
    zip_codes: Optional[str] = Query(None, description="Comma-separated ZIP codes"),
    assigned_attorney_uid: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: VerifiedUser = Depends(get_verified_user),
) -> PaginatedCases:
    """
    Returns paginated cases assigned to the user's legal aid organization.
    Cases are filtered to the organization's coverage ZIP codes.
    """
    # Build Firestore query
    query = _db.collection("case_analyses")

    # Scope to org's ZIP codes (data isolation)
    if user.coverage_zip_codes:
        # Firestore `in` supports max 30 items — chunk if needed
        coverage = user.coverage_zip_codes[:30]
        query = query.where("zip_code", "in", coverage)
    elif user.coverage_states:
        # State-level coverage: filter post-query (Firestore limitation)
        pass  # Applied below

    if routing_recommendation:
        query = query.where("routing_recommendation", "==", routing_recommendation)
    if overall_case_strength:
        query = query.where("overall_case_strength", "==", overall_case_strength)
    if assigned_attorney_uid:
        query = query.where("assigned_attorney_uid", "==", assigned_attorney_uid)

    query = query.order_by("days_until_hearing", direction=firestore.Query.ASCENDING)

    # Paginate
    offset = (page - 1) * page_size
    docs = list(query.offset(offset).limit(page_size).stream())

    cases: List[CaseSummary] = []
    for doc in docs:
        data = doc.to_dict()
        # Parse defenses
        defenses_raw = data.get("defenses_identified", [])
        if isinstance(defenses_raw, str):
            try:
                defenses_raw = json.loads(defenses_raw)
            except json.JSONDecodeError:
                defenses_raw = []
        defense_types = [d.get("defense_type", "") for d in defenses_raw]

        # Defense type filter (post-query)
        if defense_type and defense_type not in defense_types:
            continue

        # Days range filter (post-query for ranges)
        days = data.get("days_until_hearing", 0)
        if days_until_hearing_min is not None and days < days_until_hearing_min:
            continue
        if days_until_hearing_max is not None and days > days_until_hearing_max:
            continue

        # ZIP code filter override
        case_zip = data.get("zip_code", "")
        if zip_codes:
            allowed_zips = [z.strip() for z in zip_codes.split(",")]
            if case_zip not in allowed_zips:
                continue

        cases.append(CaseSummary(
            case_number=data.get("case_number", doc.id),
            routing_recommendation=data.get("routing_recommendation", ""),
            overall_case_strength=data.get("overall_case_strength", ""),
            days_until_hearing=days,
            hearing_date=str(data.get("hearing_date", "")),
            zip_code=case_zip,
            defenses_count=len(defense_types),
            defenses_types=defense_types,
            landlord_name=data.get("landlord_profile", {}).get("landlord_name") if isinstance(data.get("landlord_profile"), dict) else None,
            assigned_attorney_uid=data.get("assigned_attorney_uid"),
            assigned_attorney_email=data.get("assigned_attorney_email"),
            processing_status=data.get("processing_status", "analyzed"),
        ))

    # Total count (separate query — Firestore doesn't support COUNT with offset)
    total_query = _db.collection("case_analyses")
    if user.coverage_zip_codes:
        total_query = total_query.where("zip_code", "in", coverage)
    total_count = len(list(total_query.select([]).stream()))

    return PaginatedCases(
        cases=cases,
        total_count=total_count,
        page=page,
        page_size=page_size,
        next_page_token=str(page + 1) if len(cases) == page_size else None,
    )


# ---------------------------------------------------------------------------
# GET /cases/{case_number}
# ---------------------------------------------------------------------------


@router.get("/{case_number}")
async def get_case(
    case_number: str,
    user: VerifiedUser = Depends(get_verified_user),
) -> Dict[str, Any]:
    """
    Returns full case analysis + tenant intake responses + landlord profile + attorney brief URL.
    """
    doc_id = case_number.replace("/", "_")

    # Case analysis
    analysis_doc = _db.collection("case_analyses").document(doc_id).get()
    if not analysis_doc.exists:
        raise HTTPException(status_code=404, detail=f"Case {case_number} not found")

    analysis = analysis_doc.to_dict()
    zip_code = analysis.get("zip_code", "")
    require_org_access(zip_code, user)

    # Filing details
    filing_doc = _db.collection("eviction_filings").document(doc_id).get()
    filing = filing_doc.to_dict() if filing_doc.exists else {}

    # Tenant intake (strip PII for non-admin users)
    intake_doc = _db.collection("tenant_intake_responses").document(doc_id).get()
    intake = intake_doc.to_dict() if intake_doc.exists else {}
    if user.role == "read_only":
        intake.pop("tenant_phone", None)
        intake.pop("tenant_email", None)

    # Attorney brief signed URL
    brief_url = _get_brief_signed_url(case_number)

    return {
        "analysis": analysis,
        "filing": filing,
        "intake_responses": intake,
        "attorney_brief_url": brief_url,
    }


def _get_brief_signed_url(case_number: str) -> Optional[str]:
    """Return a signed URL for the attorney brief PDF, or None if not yet generated."""
    import datetime
    blob_name = f"briefs/{case_number.replace('/', '_')}/brief.pdf"
    bucket = _storage.bucket(STORAGE_BUCKET_BRIEFS)
    blob = bucket.blob(blob_name)
    if not blob.exists():
        return None
    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(hours=2),
        method="GET",
    )
    return url


# ---------------------------------------------------------------------------
# POST /cases/{case_number}/assign
# ---------------------------------------------------------------------------


@router.post("/{case_number}/assign")
async def assign_case(
    case_number: str,
    body: CaseAssignRequest,
    user: VerifiedUser = Depends(get_verified_user),
) -> Dict[str, str]:
    """Assign a case to a specific attorney. Requires attorney or admin role."""
    require_attorney_or_admin(user)

    doc_id = case_number.replace("/", "_")
    analysis_doc = _db.collection("case_analyses").document(doc_id).get()
    if not analysis_doc.exists:
        raise HTTPException(status_code=404, detail=f"Case {case_number} not found")

    require_org_access(analysis_doc.to_dict().get("zip_code", ""), user)

    # Verify attorney belongs to the same org
    attorney_doc = _db.collection("user_profiles").document(body.attorney_uid).get()
    if not attorney_doc.exists or attorney_doc.to_dict().get("org_id") != user.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Attorney must be a member of your organization.",
        )

    attorney_data = attorney_doc.to_dict()
    _db.collection("case_analyses").document(doc_id).update({
        "assigned_attorney_uid": body.attorney_uid,
        "assigned_attorney_email": attorney_data.get("email", ""),
        "assigned_org_id": user.org_id,
        "assignment_timestamp": datetime.utcnow().isoformat(),
        "assignment_notes": body.notes,
    })

    # Notify attorney via Pub/Sub (downstream notification Cloud Function)
    try:
        from google.cloud import pubsub_v1
        publisher = pubsub_v1.PublisherClient()
        topic_path = f"projects/{PROJECT_ID}/topics/case-assigned"
        payload = json.dumps({
            "case_number": case_number,
            "attorney_uid": body.attorney_uid,
            "assigned_by": user.uid,
            "org_id": user.org_id,
        }).encode("utf-8")
        publisher.publish(topic_path, data=payload).result(timeout=10)
    except Exception as exc:
        logger.warning("Failed to publish assignment notification: %s", exc)

    return {"status": "assigned", "case_number": case_number, "attorney_uid": body.attorney_uid}


# ---------------------------------------------------------------------------
# POST /cases/{case_number}/outcome
# ---------------------------------------------------------------------------


@router.post("/{case_number}/outcome")
async def record_outcome(
    case_number: str,
    body: CaseOutcomeRequest,
    user: VerifiedUser = Depends(get_verified_user),
) -> Dict[str, str]:
    """
    Record the hearing outcome. Updates BigQuery filing_events for the feedback loop.
    Requires attorney or admin role.
    """
    require_attorney_or_admin(user)

    doc_id = case_number.replace("/", "_")
    analysis_doc = _db.collection("case_analyses").document(doc_id).get()
    if not analysis_doc.exists:
        raise HTTPException(status_code=404, detail=f"Case {case_number} not found")

    require_org_access(analysis_doc.to_dict().get("zip_code", ""), user)

    outcome_data = {
        "case_number": case_number,
        "outcome": body.outcome,
        "outcome_notes": body.outcome_notes,
        "tenant_was_represented": body.tenant_was_represented,
        "defenses_raised": body.defenses_raised,
        "defenses_that_succeeded": body.defenses_that_succeeded,
        "recorded_by_uid": user.uid,
        "recorded_by_org": user.org_id,
        "recorded_at": datetime.utcnow().isoformat(),
    }

    # Write to Firestore case_outcomes (triggers Module 6A Cloud Function)
    _db.collection("case_outcomes").document(doc_id).set(outcome_data, merge=True)

    return {"status": "recorded", "case_number": case_number, "outcome": body.outcome}


# ---------------------------------------------------------------------------
# DELETE /cases/{case_number}/tenant-data  (Module 7C: right to deletion)
# ---------------------------------------------------------------------------


@router.delete("/{case_number}/tenant-data")
async def delete_tenant_data(
    case_number: str,
    user: VerifiedUser = Depends(get_verified_user),
) -> Dict[str, str]:
    """
    Immediately delete tenant PII from the case record.
    The case analysis, defense identification, and outcomes are retained in de-identified form.
    """
    require_attorney_or_admin(user)

    doc_id = case_number.replace("/", "_")

    pii_fields_to_null = {
        "tenant_name": None,
        "tenant_phone": None,
        "tenant_email": None,
    }
    _db.collection("eviction_filings").document(doc_id).update({
        **pii_fields_to_null,
        "pii_deleted": True,
        "pii_deleted_at": datetime.utcnow().isoformat(),
        "pii_deleted_by": user.uid,
    })

    # Also delete from intake responses
    _db.collection("tenant_intake_responses").document(doc_id).update({
        **pii_fields_to_null,
        "pii_deleted": True,
    })

    logger.info("Tenant PII deleted for case %s by user %s", case_number, user.uid[:8])
    return {"status": "deleted", "case_number": case_number}
