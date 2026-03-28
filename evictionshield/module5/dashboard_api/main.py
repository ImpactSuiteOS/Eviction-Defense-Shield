"""
EvictionShield — Module 5A: Legal Aid Organization Dashboard API

FastAPI backend deployed on Cloud Run.
Authenticated via Google Cloud Identity Platform (Firebase Auth) JWT tokens.
Enforces organization-level data isolation on every endpoint.

Environment variables:
    GCP_PROJECT_ID
    FIRESTORE_DATABASE           (default: (default))
    BIGQUERY_DATASET             (default: evictionshield)
    FIREBASE_PROJECT_ID          (may differ from GCP_PROJECT_ID)
    STORAGE_BUCKET_BRIEFS        Cloud Storage bucket for attorney briefs
    PORT                         (default: 8080)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.cloud import bigquery, firestore, storage
from pydantic import BaseModel, Field

from .auth import get_verified_user, VerifiedUser, require_org_access
from .routers import analytics, cases, landlords

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
BQ_DATASET: str = os.environ.get("BIGQUERY_DATASET", "evictionshield")
STORAGE_BUCKET_BRIEFS: str = os.environ.get("STORAGE_BUCKET_BRIEFS", "evictionshield-attorney-briefs")
PORT: int = int(os.environ.get("PORT", "8080"))

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="EvictionShield Legal Aid Dashboard API",
    version="1.0.0",
    description=(
        "API for legal aid organizations to triage and manage eviction defense cases. "
        "All endpoints require JWT authentication. Data is isolated per organization."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.evictionshield.io", "https://dashboard.evictionshield.io"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# Include routers
app.include_router(cases.router, prefix="/cases", tags=["cases"])
app.include_router(landlords.router, prefix="/landlords", tags=["landlords"])
app.include_router(analytics.router, prefix="/analytics", tags=["analytics"])


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "service": "evictionshield-dashboard-api", "version": "1.0.0"}
