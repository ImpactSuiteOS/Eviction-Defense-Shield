"""
EvictionShield — Module 2D: Jurisdiction Ruleset Loader

Loads jurisdiction-specific legal rules from YAML files stored in Cloud Storage.
Dynamically injects the correct ruleset into the Gemini system prompt at inference time.

YAML files are stored at: gs://{GCS_BUCKET_RULESETS}/rulesets/{jurisdiction_code}.yaml
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import yaml
from google.cloud import storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YAML Schema Definition (enforced at load time)
# ---------------------------------------------------------------------------

REQUIRED_RULESET_FIELDS = {
    "jurisdiction_name",
    "jurisdiction_code",
    "state",
    "notice_periods",
    "delivery_methods",
    "mandatory_filing_disclosures",
    "retaliation_window_days",
    "citation_format",
    "licensure_required",
}

REQUIRED_NOTICE_PERIOD_FIELDS = {"lease_type", "reason", "minimum_days", "statute"}


class InsufficientRulesError(Exception):
    """Raised when a loaded ruleset is missing fields required for reliable analysis."""

    def __init__(self, jurisdiction_code: str, missing_fields: List[str]):
        self.jurisdiction_code = jurisdiction_code
        self.missing_fields = missing_fields
        super().__init__(
            f"Ruleset for '{jurisdiction_code}' is missing required fields: {missing_fields}"
        )


# ---------------------------------------------------------------------------
# JurisdictionRuleset
# ---------------------------------------------------------------------------


class JurisdictionRuleset:
    """
    Loads, validates, and renders jurisdiction-specific legal rules.

    Caches loaded rulesets in memory for the lifetime of the Cloud Function
    instance to avoid repeated GCS reads on warm invocations.
    """

    def __init__(self, project_id: str, gcs_bucket: str):
        self._project_id = project_id
        self._gcs_bucket = gcs_bucket
        self._storage_client = storage.Client(project=project_id)
        self._cache: Dict[str, Dict[str, Any]] = {}

    def _load_from_gcs(self, jurisdiction_code: str) -> Dict[str, Any]:
        """
        Download and parse the YAML ruleset file for a jurisdiction.
        Raises FileNotFoundError if the object does not exist.
        """
        blob_name = f"rulesets/{jurisdiction_code}.yaml"
        bucket = self._storage_client.bucket(self._gcs_bucket)
        blob = bucket.blob(blob_name)
        if not blob.exists():
            raise FileNotFoundError(
                f"No ruleset file found for jurisdiction '{jurisdiction_code}' "
                f"at gs://{self._gcs_bucket}/{blob_name}"
            )
        raw_yaml = blob.download_as_text(encoding="utf-8")
        ruleset = yaml.safe_load(raw_yaml)
        self._cache[jurisdiction_code] = ruleset
        logger.info("Loaded ruleset for %s from GCS", jurisdiction_code)
        return ruleset

    def get_ruleset(self, jurisdiction_code: str) -> Dict[str, Any]:
        """Return the ruleset dict, loading from GCS if not cached."""
        if jurisdiction_code in self._cache:
            return self._cache[jurisdiction_code]
        return self._load_from_gcs(jurisdiction_code)

    def validate_ruleset(self, jurisdiction_code: str) -> None:
        """
        Check that the ruleset contains all fields required for reliable analysis.
        Raises InsufficientRulesError listing specific missing fields.
        """
        ruleset = self.get_ruleset(jurisdiction_code)
        missing: List[str] = []

        for field in REQUIRED_RULESET_FIELDS:
            if field not in ruleset or ruleset[field] is None:
                missing.append(field)

        # Validate notice periods structure
        notice_periods = ruleset.get("notice_periods", [])
        if not notice_periods:
            missing.append("notice_periods[0]")
        else:
            for i, np in enumerate(notice_periods[:3]):  # Spot-check first 3
                for subfield in REQUIRED_NOTICE_PERIOD_FIELDS:
                    if subfield not in np:
                        missing.append(f"notice_periods[{i}].{subfield}")

        if missing:
            raise InsufficientRulesError(jurisdiction_code, missing)

    def render_for_prompt(self, jurisdiction_code: str) -> str:
        """
        Render the jurisdiction ruleset as a formatted string for injection
        into the Gemini system prompt.
        """
        ruleset = self.get_ruleset(jurisdiction_code)

        # Notice periods section
        notice_lines = []
        for np in ruleset.get("notice_periods", []):
            notice_lines.append(
                f"  - Lease type: {np.get('lease_type')} | Reason: {np.get('reason')} | "
                f"Minimum: {np.get('minimum_days')} days | Statute: {np.get('statute')}"
            )
        notice_section = "\n".join(notice_lines) if notice_lines else "  Not specified"

        # Delivery methods
        delivery_lines = []
        for dm in ruleset.get("delivery_methods", []):
            delivery_lines.append(
                f"  - {dm.get('method')}: {dm.get('description')} | "
                f"Proof required: {dm.get('proof_required', 'not specified')}"
            )
        delivery_section = "\n".join(delivery_lines) if delivery_lines else "  Not specified"

        # Mandatory disclosures
        disclosure_lines = []
        for d in ruleset.get("mandatory_filing_disclosures", []):
            disclosure_lines.append(f"  - {d.get('disclosure')}: {d.get('statute', '')}")
        disclosure_section = "\n".join(disclosure_lines) if disclosure_lines else "  None specified"

        # Local ordinances
        ordinance_lines = []
        for o in ruleset.get("local_ordinances", []):
            ordinance_lines.append(
                f"  - {o.get('ordinance_name')}: {o.get('description')} "
                f"[Jurisdiction: {o.get('applies_to', 'statewide')}]"
            )
        ordinance_section = "\n".join(ordinance_lines) if ordinance_lines else "  None"

        # Moratoriums
        moratorium_lines = []
        for m in ruleset.get("current_moratoriums", []):
            if m.get("active", False):
                moratorium_lines.append(
                    f"  - {m.get('name')}: {m.get('description')} | "
                    f"Expires: {m.get('expiration_date', 'ongoing')}"
                )
        moratorium_section = (
            "\n".join(moratorium_lines) if moratorium_lines else "  No active moratoriums"
        )

        # Licensure
        licensure_required = ruleset.get("licensure_required", False)
        licensure_details = ""
        if licensure_required:
            licensure_details = f"  Details: {ruleset.get('licensure_details', 'Required by local ordinance')}"

        return f"""JURISDICTION-SPECIFIC RULES FOR {ruleset.get('jurisdiction_name')} ({jurisdiction_code}):

NOTICE PERIODS (minimum days required by statute):
{notice_section}

PERMITTED NOTICE DELIVERY METHODS:
{delivery_section}

MANDATORY FILING DISCLOSURES:
{disclosure_section}

LOCAL ORDINANCES AND SPECIAL RULES:
{ordinance_section}

RETALIATION WINDOW: {ruleset.get('retaliation_window_days', 180)} days from protected activity

STATUTE CITATION FORMAT: {ruleset.get('citation_format', 'State Code § Section')}

LANDLORD LICENSURE REQUIRED: {licensure_required}
{licensure_details}

CURRENT MORATORIUMS:
{moratorium_section}

COURT HIERARCHY: {ruleset.get('court_hierarchy', 'Not specified')}

SPECIAL TENANT PROTECTIONS:
{chr(10).join('  - ' + p for p in ruleset.get('special_tenant_protections', ['None specified']))}
"""
