"""
EvictionShield — Module 5A: JWT Authentication Middleware

Uses Firebase Auth (Google Cloud Identity Platform) for token verification.
Enforces organization-level data isolation: each legal aid org can only see
cases assigned to their geographic coverage area.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import firebase_admin
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as firebase_auth, credentials, firestore as firebase_firestore
from google.cloud import firestore

logger = logging.getLogger(__name__)

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]

# Initialize Firebase Admin SDK (uses Application Default Credentials on Cloud Run)
_firebase_app: Optional[firebase_admin.App] = None


def _get_firebase_app() -> firebase_admin.App:
    global _firebase_app
    if _firebase_app is None:
        _firebase_app = firebase_admin.initialize_app(
            options={"projectId": PROJECT_ID}
        )
    return _firebase_app


_bearer = HTTPBearer(auto_error=True)
_db = firestore.Client(project=PROJECT_ID)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class VerifiedUser:
    uid: str
    email: str
    org_id: str
    org_name: str
    role: str                    # admin | attorney | read_only
    coverage_zip_codes: list[str]
    coverage_states: list[str]


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


async def get_verified_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> VerifiedUser:
    """
    Verify Firebase Auth JWT token and load organization membership.
    Raises 401 on invalid token, 403 if user has no organization.
    """
    token = credentials.credentials
    try:
        app = _get_firebase_app()
        decoded = firebase_auth.verify_id_token(token, app=app, check_revoked=True)
    except firebase_auth.RevokedIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please sign in again.",
        )
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired. Please sign in again.",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {exc}",
        )

    uid = decoded["uid"]
    email = decoded.get("email", "")

    # Load org membership from Firestore
    user_doc = _db.collection("user_profiles").document(uid).get()
    if not user_doc.exists:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account not provisioned. Contact your administrator.",
        )

    user_data = user_doc.to_dict()
    org_id = user_data.get("org_id")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with a legal aid organization.",
        )

    # Load org coverage settings
    org_doc = _db.collection("legal_aid_orgs").document(org_id).get()
    if not org_doc.exists:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization record not found.",
        )
    org_data = org_doc.to_dict()

    return VerifiedUser(
        uid=uid,
        email=email,
        org_id=org_id,
        org_name=org_data.get("organization_name", ""),
        role=user_data.get("role", "read_only"),
        coverage_zip_codes=org_data.get("coverage_zip_codes", []),
        coverage_states=org_data.get("coverage_states", []),
    )


def require_org_access(case_zip_code: str, user: VerifiedUser) -> None:
    """
    Verify the user's organization has coverage for the given ZIP code.
    Raises 403 if the case falls outside their coverage area.
    """
    if not user.coverage_zip_codes and not user.coverage_states:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization has no coverage area configured.",
        )

    # ZIP-level check (most specific)
    if user.coverage_zip_codes and case_zip_code in user.coverage_zip_codes:
        return

    # State-level fallback
    if user.coverage_states:
        # ZIP to state mapping via prefix (simplified — production should use full ZIP DB)
        zip_prefix = int(case_zip_code[:3]) if case_zip_code[:3].isdigit() else -1
        state = _zip_prefix_to_state(zip_prefix)
        if state in user.coverage_states:
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"Case ZIP code {case_zip_code} is outside your organization's coverage area. "
            "Contact your administrator to expand coverage."
        ),
    )


def require_attorney_or_admin(user: VerifiedUser) -> None:
    if user.role not in ("attorney", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires attorney or admin role.",
        )


def _zip_prefix_to_state(prefix: int) -> str:
    if 150 <= prefix <= 196:
        return "PA"
    if 900 <= prefix <= 961:
        return "CA"
    if 100 <= prefix <= 119:
        return "NY"
    if 600 <= prefix <= 627:
        return "IL"
    return "UNKNOWN"
