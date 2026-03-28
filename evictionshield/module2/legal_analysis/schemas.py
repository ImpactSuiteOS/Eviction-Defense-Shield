"""
EvictionShield — Module 2B: Pydantic Schemas for Gemini Legal Analysis Output

All Gemini responses are validated against these models before being written to
Firestore or forwarded to tenants. Schema validation failures trigger an
automatic correction prompt (one retry) before raising.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


LEGAL_DISCLAIMER = (
    "This is legal information, not legal advice. The analysis above identifies "
    "potential procedural issues and defenses based on the information available in "
    "the court filing and public records. It does not constitute legal representation, "
    "predict the outcome of your case, or substitute for advice from a licensed attorney. "
    "Please consult a qualified attorney before taking any action on your case. "
    "Free legal aid may be available — see the referral information provided."
)


class DefenseIdentified(BaseModel):
    defense_type: Literal[
        "improper_notice_period",
        "defective_notice_delivery",
        "missing_required_disclosures",
        "retaliatory_eviction",
        "waiver_by_rent_acceptance",
        "habitability_defense",
        "landlord_licensure_deficiency",
        "discriminatory_filing_pattern",
        "procedural_defect_summons",
        "improper_plaintiff",
        "section8_procedural_violation",
        "moratorium_applicability",
    ]
    confidence: Literal["high", "medium", "low"]
    legal_basis: str = Field(
        ...,
        description="Cite the specific statute section or procedural rule. Example: '68 Pa. C.S. § 250.501(b)'",
        min_length=5,
    )
    evidence_in_filing: str = Field(
        ...,
        description="Specific text from the filing or record that supports this defense.",
        min_length=10,
    )
    recommended_action: str = Field(
        ...,
        description="Concrete action the tenant or their attorney should take.",
        min_length=10,
    )
    time_sensitive: bool = Field(
        ...,
        description="True if this defense must be raised before or at the hearing to avoid waiver.",
    )
    deadline_days: Optional[int] = Field(
        None,
        ge=0,
        description="Number of days from today by which action must be taken. Null if not time-bounded.",
    )

    @field_validator("legal_basis")
    @classmethod
    def legal_basis_must_cite_law(cls, v: str) -> str:
        # Must contain at least one statutory indicator
        import re
        pattern = r"(§|section|rule|code|c\.s\.|usc|cfr|ord\.|42 u\.s\.c|68 pa\.)"
        if not re.search(pattern, v, re.IGNORECASE):
            raise ValueError(
                f"legal_basis must cite a specific statute, rule, or ordinance. Got: '{v}'"
            )
        return v


class LandlordProfile(BaseModel):
    landlord_name: str
    entity_type: str = Field(
        ...,
        description="individual | LLC | corporation | property_mgmt_company | unknown",
    )
    filing_count_90_days: int = Field(..., ge=0)
    withdrawal_rate_pct: float = Field(..., ge=0.0, le=100.0)
    prior_habitability_complaints: int = Field(..., ge=0)
    pattern_flag: Optional[str] = Field(
        None,
        description="e.g. 'high_volume_filer', 'high_withdrawal_rate', 'retaliation_pattern'",
    )


class TenantActionPlan(BaseModel):
    immediate_actions: List[str] = Field(
        ...,
        description="Actions the tenant should take today.",
        min_length=1,
    )
    before_hearing_actions: List[str] = Field(
        ...,
        description="Actions the tenant should complete before the hearing date.",
        min_length=1,
    )
    documents_to_gather: List[str] = Field(
        ...,
        description="Physical and digital documents the tenant should collect.",
        min_length=1,
    )
    questions_to_ask_attorney: List[str] = Field(
        ...,
        description="Specific questions the tenant should ask if they consult an attorney.",
        min_length=1,
    )
    legal_aid_referral_priority: Literal["urgent", "standard", "informational"] = Field(
        ...,
        description="urgent = hearing within 5 days or high-confidence defense; standard = 6-14 days; informational = 15+ days or weak defenses",
    )


class DefenseSuccessProbability(BaseModel):
    """Injected from BigQuery ML model at inference time."""
    defense_type: str
    jurisdiction: str
    success_probability_pct: float = Field(..., ge=0.0, le=100.0)
    sample_size: int = Field(..., ge=0, description="Number of historical cases used in estimate")
    represented_only: bool = Field(
        True,
        description="True if probability is conditioned on tenant being represented by counsel",
    )


class EvictionAnalysisResult(BaseModel):
    case_number: str = Field(..., min_length=3)
    analysis_timestamp: datetime
    jurisdiction: str
    defenses_identified: List[DefenseIdentified] = Field(default_factory=list)
    overall_case_strength: Literal["strong", "moderate", "weak", "insufficient_data"]
    landlord_profile: LandlordProfile
    tenant_action_plan: TenantActionPlan
    hearing_date: date
    days_until_hearing: int = Field(..., ge=0)
    routing_recommendation: Literal[
        "urgent_legal_aid",
        "standard_legal_aid",
        "self_help_resources",
        "monitor_only",
    ]
    defense_probabilities: List[DefenseSuccessProbability] = Field(
        default_factory=list,
        description="BigQuery ML predicted success probabilities for each identified defense.",
    )
    disclaimer: str = Field(
        default=LEGAL_DISCLAIMER,
        description="Always populated. Legal information vs. legal advice disclaimer.",
    )

    @model_validator(mode="after")
    def disclaimer_must_be_populated(self) -> "EvictionAnalysisResult":
        if not self.disclaimer or len(self.disclaimer) < 50:
            self.disclaimer = LEGAL_DISCLAIMER
        return self

    @model_validator(mode="after")
    def routing_matches_urgency(self) -> "EvictionAnalysisResult":
        """
        Enforce: if hearing is within 5 days, routing must be urgent_legal_aid
        unless overall_case_strength is insufficient_data.
        """
        if (
            self.days_until_hearing <= 5
            and self.overall_case_strength != "insufficient_data"
            and self.routing_recommendation not in ("urgent_legal_aid",)
        ):
            self.routing_recommendation = "urgent_legal_aid"
        return self

    @model_validator(mode="after")
    def days_until_hearing_is_consistent(self) -> "EvictionAnalysisResult":
        today = datetime.utcnow().date()
        computed = (self.hearing_date - today).days
        # Allow ±1 day tolerance for timezone edge cases
        if abs(computed - self.days_until_hearing) > 1:
            self.days_until_hearing = max(0, computed)
        return self

    def to_firestore_dict(self) -> dict:
        """Serialize for Firestore (converts dates to ISO strings)."""
        return self.model_dump(mode="json")
