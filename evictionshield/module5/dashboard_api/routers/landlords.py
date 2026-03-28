"""EvictionShield — Module 5A: Landlords Router"""
from __future__ import annotations
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from google.cloud import bigquery

from ..auth import VerifiedUser, get_verified_user

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
BQ_DATASET = os.environ.get("BIGQUERY_DATASET", "evictionshield")

_bq = bigquery.Client(project=PROJECT_ID)
router = APIRouter()


@router.get("/{landlord_id}")
async def get_landlord(
    landlord_id: str,
    user: VerifiedUser = Depends(get_verified_user),
) -> Dict[str, Any]:
    """Returns full landlord behavioral profile from BigQuery landlord_risk_scores view."""
    query = f"""
        SELECT *
        FROM `{PROJECT_ID}.{BQ_DATASET}.landlord_risk_scores`
        WHERE landlord_id = @landlord_id
        LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("landlord_id", "STRING", landlord_id)]
    )
    rows = list(_bq.query(query, job_config=job_config).result())
    if not rows:
        raise HTTPException(status_code=404, detail=f"Landlord {landlord_id} not found")

    row = dict(rows[0])
    # Convert non-serializable types
    for k, v in row.items():
        if hasattr(v, 'isoformat'):
            row[k] = v.isoformat()
    return row
