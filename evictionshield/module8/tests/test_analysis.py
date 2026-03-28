"""
EvictionShield — Module 8: Gemini Analysis Engine Tests

Tests:
- Pydantic schema validation
- Defense type identification in fixture filings
- System prompt construction
- Jurisdiction ruleset loading and validation
- Gemini output parsing and correction flow
- Disclaimer always populated
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from module2.legal_analysis.schemas import (
    DefenseIdentified,
    EvictionAnalysisResult,
    LandlordProfile,
    TenantActionPlan,
    LEGAL_DISCLAIMER,
)
from module2.legal_analysis.system_prompt import build_system_prompt, build_correction_prompt
from .fixtures.filings import (
    FIXTURE_CLEAN,
    FIXTURE_IMPROPER_NOTICE,
    FIXTURE_RETALIATION,
    FIXTURE_SECTION8,
    FIXTURE_ENTITY_MISMATCH,
    ALL_FIXTURES,
)


# ---------------------------------------------------------------------------
# Schema Validation Tests
# ---------------------------------------------------------------------------

class TestEvictionAnalysisResultSchema:
    """Test that the Pydantic schema validates correctly."""

    def _valid_result(self, **overrides) -> dict:
        base = {
            "case_number": "PA-MDJ-2024-000001",
            "analysis_timestamp": datetime.utcnow().isoformat(),
            "jurisdiction": "PA-UJS",
            "defenses_identified": [],
            "overall_case_strength": "weak",
            "landlord_profile": {
                "landlord_name": "Test LLC",
                "entity_type": "LLC",
                "filing_count_90_days": 5,
                "withdrawal_rate_pct": 20.0,
                "prior_habitability_complaints": 0,
                "pattern_flag": None,
            },
            "tenant_action_plan": {
                "immediate_actions": ["Call a legal aid organization today"],
                "before_hearing_actions": ["Gather rent receipts"],
                "documents_to_gather": ["Lease agreement"],
                "questions_to_ask_attorney": ["What are my options?"],
                "legal_aid_referral_priority": "standard",
            },
            "hearing_date": date.today().isoformat(),
            "days_until_hearing": 14,
            "routing_recommendation": "standard_legal_aid",
            "disclaimer": LEGAL_DISCLAIMER,
        }
        base.update(overrides)
        return base

    def test_valid_result_parses_successfully(self):
        result = EvictionAnalysisResult(**self._valid_result())
        assert result.case_number == "PA-MDJ-2024-000001"
        assert result.disclaimer == LEGAL_DISCLAIMER

    def test_disclaimer_always_populated_if_empty(self):
        """If disclaimer is missing from Gemini output, model validator must fill it in."""
        data = self._valid_result(disclaimer="")
        result = EvictionAnalysisResult(**data)
        assert result.disclaimer == LEGAL_DISCLAIMER

    def test_invalid_overall_strength_raises(self):
        with pytest.raises(ValidationError):
            EvictionAnalysisResult(**self._valid_result(overall_case_strength="excellent"))

    def test_invalid_routing_raises(self):
        with pytest.raises(ValidationError):
            EvictionAnalysisResult(**self._valid_result(routing_recommendation="maybe_legal_aid"))

    def test_urgent_hearing_forces_urgent_routing(self):
        """Hearing in 3 days with identified defenses must route to urgent_legal_aid."""
        data = self._valid_result(
            days_until_hearing=3,
            hearing_date=(date.today().__class__.today().__class__.__new__(date.today().__class__) if False else date.today()).isoformat(),
            routing_recommendation="standard_legal_aid",
            overall_case_strength="moderate",
        )
        result = EvictionAnalysisResult(**data)
        assert result.routing_recommendation == "urgent_legal_aid"

    def test_defense_legal_basis_must_cite_law(self):
        with pytest.raises(ValidationError):
            DefenseIdentified(
                defense_type="improper_notice_period",
                confidence="high",
                legal_basis="The landlord gave too little notice",  # No statute citation
                evidence_in_filing="Notice given only 6 days before filing",
                recommended_action="Raise at hearing",
                time_sensitive=True,
                deadline_days=None,
            )

    def test_defense_legal_basis_valid_with_statute(self):
        d = DefenseIdentified(
            defense_type="improper_notice_period",
            confidence="high",
            legal_basis="68 Pa. C.S. § 250.501(b)(2) requires 10-day minimum notice",
            evidence_in_filing="Notice dated June 2 filed June 8 = 6 days",
            recommended_action="Raise improper notice as defense at hearing",
            time_sensitive=True,
            deadline_days=0,
        )
        assert d.confidence == "high"

    def test_all_12_defense_types_are_valid(self):
        defense_types = [
            "improper_notice_period", "defective_notice_delivery",
            "missing_required_disclosures", "retaliatory_eviction",
            "waiver_by_rent_acceptance", "habitability_defense",
            "landlord_licensure_deficiency", "discriminatory_filing_pattern",
            "procedural_defect_summons", "improper_plaintiff",
            "section8_procedural_violation", "moratorium_applicability",
        ]
        for dt in defense_types:
            d = DefenseIdentified(
                defense_type=dt,
                confidence="low",
                legal_basis="Test § 1.1",
                evidence_in_filing="Test evidence in the filing record",
                recommended_action="Consult an attorney",
                time_sensitive=False,
                deadline_days=None,
            )
            assert d.defense_type == dt

    def test_to_firestore_dict_is_json_serializable(self):
        result = EvictionAnalysisResult(**self._valid_result())
        d = result.to_firestore_dict()
        # Must serialize without error
        json.dumps(d)


# ---------------------------------------------------------------------------
# System Prompt Construction
# ---------------------------------------------------------------------------

class TestSystemPromptConstruction:
    """Test that system prompt assembly works with real ruleset data."""

    def test_build_system_prompt_contains_defense_types(self):
        ruleset_section = "TEST JURISDICTION RULES: Notice period: 15 days."
        prompt = build_system_prompt(ruleset_section)
        assert "improper_notice_period" in prompt
        assert "retaliatory_eviction" in prompt
        assert "section8_procedural_violation" in prompt

    def test_build_system_prompt_contains_disclaimer_instruction(self):
        prompt = build_system_prompt("TEST RULES")
        assert "legal information" in prompt.lower()
        assert "legal advice" in prompt.lower()

    def test_build_system_prompt_contains_output_schema(self):
        prompt = build_system_prompt("TEST RULES")
        assert "overall_case_strength" in prompt
        assert "routing_recommendation" in prompt
        assert "defenses_identified" in prompt

    def test_correction_prompt_contains_error(self):
        prompt = build_correction_prompt(
            validation_error="Field 'routing_recommendation' is required",
            previous_response='{"case_number": "PA-001"}',
        )
        assert "routing_recommendation" in prompt
        assert "PA-001" in prompt


# ---------------------------------------------------------------------------
# Jurisdiction Ruleset Tests
# ---------------------------------------------------------------------------

class TestJurisdictionRuleset:
    """Test ruleset loading, validation, and prompt rendering."""

    @patch("module2.legal_analysis.jurisdiction_ruleset.storage")
    def test_pa_ruleset_loads_and_validates(self, mock_storage):
        """PA ruleset YAML must pass validation."""
        import yaml

        # Load the actual PA YAML file from the project
        with open("module2/rulesets/PA-UJS.yaml", "r") as f:
            pa_yaml = f.read()

        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = pa_yaml
        mock_storage.Client.return_value.bucket.return_value.blob.return_value = mock_blob

        from module2.legal_analysis.jurisdiction_ruleset import JurisdictionRuleset, InsufficientRulesError
        loader = JurisdictionRuleset(project_id="test", gcs_bucket="test-bucket")

        # Should not raise
        loader.validate_ruleset("PA-UJS")

    @patch("module2.legal_analysis.jurisdiction_ruleset.storage")
    def test_incomplete_ruleset_raises_insufficient_rules_error(self, mock_storage):
        """Rulesets missing required fields must raise InsufficientRulesError."""
        incomplete_yaml = """
jurisdiction_name: "Test State"
jurisdiction_code: "XX-TEST"
state: "XX"
# Missing: notice_periods, delivery_methods, etc.
"""
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = incomplete_yaml
        mock_storage.Client.return_value.bucket.return_value.blob.return_value = mock_blob

        from module2.legal_analysis.jurisdiction_ruleset import JurisdictionRuleset, InsufficientRulesError
        loader = JurisdictionRuleset(project_id="test", gcs_bucket="test-bucket")

        with pytest.raises(InsufficientRulesError) as exc_info:
            loader.validate_ruleset("XX-TEST")
        assert "notice_periods" in str(exc_info.value)

    @patch("module2.legal_analysis.jurisdiction_ruleset.storage")
    def test_render_for_prompt_contains_key_sections(self, mock_storage):
        """Rendered ruleset section must contain notice periods and delivery methods."""
        import yaml
        with open("module2/rulesets/PA-UJS.yaml", "r") as f:
            pa_yaml = f.read()

        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = pa_yaml
        mock_storage.Client.return_value.bucket.return_value.blob.return_value = mock_blob

        from module2.legal_analysis.jurisdiction_ruleset import JurisdictionRuleset
        loader = JurisdictionRuleset(project_id="test", gcs_bucket="test-bucket")
        rendered = loader.render_for_prompt("PA-UJS")

        assert "NOTICE PERIODS" in rendered
        assert "DELIVERY METHODS" in rendered
        assert "68 Pa. C.S." in rendered
        assert "180" in rendered  # Retaliation window


# ---------------------------------------------------------------------------
# Analysis Engine Integration Tests (Gemini mocked)
# ---------------------------------------------------------------------------

class TestAnalysisEngine:
    """Test the full analysis pipeline with Gemini mocked."""

    def _build_mock_gemini_response(self, defenses: list, strength: str = "moderate") -> str:
        """Build a valid JSON string mimicking Gemini output."""
        result = {
            "case_number": "PA-MDJ-2024-000001",
            "analysis_timestamp": datetime.utcnow().isoformat(),
            "jurisdiction": "PA-UJS",
            "defenses_identified": defenses,
            "overall_case_strength": strength,
            "landlord_profile": {
                "landlord_name": "Test LLC",
                "entity_type": "LLC",
                "filing_count_90_days": 5,
                "withdrawal_rate_pct": 20.0,
                "prior_habitability_complaints": 0,
                "pattern_flag": None,
            },
            "tenant_action_plan": {
                "immediate_actions": ["Contact legal aid today"],
                "before_hearing_actions": ["Gather lease"],
                "documents_to_gather": ["Lease", "Rent receipts"],
                "questions_to_ask_attorney": ["What defenses apply?"],
                "legal_aid_referral_priority": "standard",
            },
            "hearing_date": "2024-07-10",
            "days_until_hearing": 14,
            "routing_recommendation": "standard_legal_aid",
            "disclaimer": LEGAL_DISCLAIMER,
        }
        return json.dumps(result)

    @patch("module2.legal_analysis.analysis_engine._call_gemini")
    @patch("module2.legal_analysis.analysis_engine._get_landlord_profile_from_bq")
    @patch("module2.legal_analysis.analysis_engine._get_defense_probabilities")
    @patch("module2.legal_analysis.analysis_engine._write_analysis_to_firestore")
    @patch("module2.legal_analysis.analysis_engine._publish_analysis_complete")
    @patch("module2.legal_analysis.analysis_engine._ruleset_loader")
    def test_improper_notice_identified(
        self,
        mock_ruleset,
        mock_publish,
        mock_firestore,
        mock_probs,
        mock_bq_profile,
        mock_gemini,
    ):
        """Analysis engine should identify improper_notice_period for Fixture 2."""
        # Setup mocks
        mock_ruleset.validate_ruleset.return_value = None
        mock_ruleset.render_for_prompt.return_value = "RULES: 10-day minimum notice"
        mock_bq_profile.return_value = {
            "landlord_name": "Fast Evict Realty Inc",
            "entity_type": "corporation",
            "filing_count_90_days": 3,
            "withdrawal_rate_pct": 10.0,
            "prior_habitability_complaints": 0,
            "pattern_flag": None,
        }
        mock_probs.return_value = []
        mock_gemini.return_value = self._build_mock_gemini_response([
            {
                "defense_type": "improper_notice_period",
                "confidence": "high",
                "legal_basis": "68 Pa. C.S. § 250.501(b)(2)",
                "evidence_in_filing": "Notice dated 2024-05-02, filing date 2024-05-08 = 6 days (minimum 10 required)",
                "recommended_action": "Raise improper notice at hearing",
                "time_sensitive": True,
                "deadline_days": 0,
            }
        ], strength="moderate")

        from module2.legal_analysis.analysis_engine import analyze_filing
        result = analyze_filing(FIXTURE_IMPROPER_NOTICE)

        assert len(result.defenses_identified) == 1
        assert result.defenses_identified[0].defense_type == "improper_notice_period"
        assert result.defenses_identified[0].confidence == "high"

    @patch("module2.legal_analysis.analysis_engine._call_gemini")
    @patch("module2.legal_analysis.analysis_engine._get_landlord_profile_from_bq")
    @patch("module2.legal_analysis.analysis_engine._get_defense_probabilities")
    @patch("module2.legal_analysis.analysis_engine._write_analysis_to_firestore")
    @patch("module2.legal_analysis.analysis_engine._publish_analysis_complete")
    @patch("module2.legal_analysis.analysis_engine._ruleset_loader")
    def test_clean_filing_produces_no_defenses(
        self, mock_ruleset, mock_publish, mock_firestore, mock_probs, mock_bq_profile, mock_gemini
    ):
        """Clean filing should produce empty defenses and weak strength."""
        mock_ruleset.validate_ruleset.return_value = None
        mock_ruleset.render_for_prompt.return_value = "RULES"
        mock_bq_profile.return_value = {
            "landlord_name": "Oak Properties LLC",
            "entity_type": "LLC",
            "filing_count_90_days": 2,
            "withdrawal_rate_pct": 5.0,
            "prior_habitability_complaints": 0,
            "pattern_flag": None,
        }
        mock_probs.return_value = []
        mock_gemini.return_value = self._build_mock_gemini_response([], strength="weak")

        from module2.legal_analysis.analysis_engine import analyze_filing
        result = analyze_filing(FIXTURE_CLEAN)
        assert result.defenses_identified == []
        assert result.overall_case_strength == "weak"
        assert result.disclaimer == LEGAL_DISCLAIMER

    @patch("module2.legal_analysis.analysis_engine._call_gemini")
    @patch("module2.legal_analysis.analysis_engine._get_landlord_profile_from_bq")
    @patch("module2.legal_analysis.analysis_engine._get_defense_probabilities")
    @patch("module2.legal_analysis.analysis_engine._write_analysis_to_firestore")
    @patch("module2.legal_analysis.analysis_engine._publish_analysis_complete")
    @patch("module2.legal_analysis.analysis_engine._ruleset_loader")
    def test_schema_validation_failure_triggers_retry(
        self, mock_ruleset, mock_publish, mock_firestore, mock_probs, mock_bq_profile, mock_gemini
    ):
        """On first Gemini call returning invalid JSON, engine should retry with correction prompt."""
        mock_ruleset.validate_ruleset.return_value = None
        mock_ruleset.render_for_prompt.return_value = "RULES"
        mock_bq_profile.return_value = {"landlord_name": "Test", "entity_type": "LLC",
                                          "filing_count_90_days": 0, "withdrawal_rate_pct": 0,
                                          "prior_habitability_complaints": 0, "pattern_flag": None}
        mock_probs.return_value = []

        # First call: invalid JSON — missing required fields
        invalid_response = '{"case_number": "PA-001"}'  # Missing many required fields
        valid_response = self._build_mock_gemini_response([], strength="insufficient_data")
        mock_gemini.side_effect = [invalid_response, valid_response]

        from module2.legal_analysis.analysis_engine import analyze_filing
        result = analyze_filing(FIXTURE_CLEAN)

        # Should have called Gemini twice
        assert mock_gemini.call_count == 2
        assert result.overall_case_strength == "insufficient_data"
