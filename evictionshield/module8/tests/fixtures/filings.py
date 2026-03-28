"""
EvictionShield — Module 8: Test Fixtures
20 synthetic eviction filings covering all 12 defense types.
All names, case numbers, and addresses are fictitious.
"""

from datetime import date, datetime

# ---------------------------------------------------------------------------
# Fixture 1: CLEAN FILING — no defects
# ---------------------------------------------------------------------------
FIXTURE_CLEAN = {
    "case_number": "PA-MDJ-2024-000001",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "court_name": "Magisterial District Court 05-2-01",
    "landlord_name": "Oak Properties LLC",
    "landlord_entity_type": "LLC",
    "landlord_address": "100 Main St, Philadelphia, PA 19103",
    "tenant_name": "Jane Smith",
    "tenant_phone": "+12155550101",
    "property_address": "500 Broad St Apt 3B, Philadelphia, PA 19147",
    "zip_code": "19147",
    "filing_date": "2024-06-15",
    "filing_reason": "Nonpayment of rent — May 2024 rent of $1,200.00 unpaid",
    "claimed_amount": 1200.00,
    "notice_date": "2024-06-01",
    "notice_type": "pay_or_quit",
    "notice_period_days": 14,
    "hearing_date": "2024-07-10",
    "document_uri": "gs://evictionshield-test/fixtures/case_000001.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-06-16T08:00:00Z",
}

FIXTURE_CLEAN_EXPECTED_DEFENSES = []
FIXTURE_CLEAN_EXPECTED_STRENGTH = "weak"


# ---------------------------------------------------------------------------
# Fixture 2: IMPROPER NOTICE PERIOD — notice too short
# ---------------------------------------------------------------------------
FIXTURE_IMPROPER_NOTICE = {
    "case_number": "PA-MDJ-2024-000002",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "court_name": "Magisterial District Court 05-2-01",
    "landlord_name": "Fast Evict Realty Inc",
    "landlord_entity_type": "corporation",
    "landlord_address": "200 Commerce Blvd, Philadelphia, PA 19103",
    "tenant_name": "Robert Johnson",
    "tenant_phone": "+12155550202",
    "property_address": "700 Market St Apt 5C, Philadelphia, PA 19106",
    "zip_code": "19106",
    "filing_date": "2024-05-08",
    "filing_reason": "Nonpayment of rent — April 2024 rent $950.00 unpaid",
    "claimed_amount": 950.00,
    "notice_date": "2024-05-02",   # DEFECT: Only 6 days before filing — PA requires 10 days minimum
    "notice_type": "pay_or_quit",
    "notice_period_days": 6,       # < 10 day minimum for nonpayment
    "hearing_date": "2024-06-01",
    "document_uri": "gs://evictionshield-test/fixtures/case_000002.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-05-09T08:00:00Z",
}

FIXTURE_IMPROPER_NOTICE_EXPECTED_DEFENSES = ["improper_notice_period"]
FIXTURE_IMPROPER_NOTICE_EXPECTED_CONFIDENCE = "high"


# ---------------------------------------------------------------------------
# Fixture 3: RETALIATORY EVICTION with habitability complaint history
# ---------------------------------------------------------------------------
FIXTURE_RETALIATION = {
    "case_number": "PA-MDJ-2024-000003",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "court_name": "Magisterial District Court 05-2-01",
    "landlord_name": "Slumlord Properties LLC",
    "landlord_entity_type": "LLC",
    "landlord_address": "300 Industrial Ave, Philadelphia, PA 19134",
    "tenant_name": "Maria Rodriguez",
    "tenant_phone": "+12155550303",
    "property_address": "1200 Kensington Ave Apt 1R, Philadelphia, PA 19125",
    "zip_code": "19125",
    "filing_date": "2024-04-10",
    "filing_reason": "Nonpayment of rent — March and April 2024, $2,400.00 total",
    "claimed_amount": 2400.00,
    "notice_date": "2024-03-28",
    "notice_type": "pay_or_quit",
    "notice_period_days": 13,
    "hearing_date": "2024-05-05",
    "document_uri": "gs://evictionshield-test/fixtures/case_000003.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-04-11T08:00:00Z",
    # Landlord profile data (would come from BigQuery in production)
    "_test_landlord_profile": {
        "landlord_name": "Slumlord Properties LLC",
        "entity_type": "LLC",
        "filing_count_90_days": 12,
        "withdrawal_rate_pct": 55.0,
        "prior_habitability_complaints": 8,
        "complaint_to_filing_correlation": 0.72,
        "pattern_flag": "retaliation_pattern",
        # Complaint filed Feb 15, 2024 — eviction filed April 10 = 54 days later
        "_test_last_complaint_date": "2024-02-15",
        "_test_complaint_category": "mold_and_water_damage",
    },
}

FIXTURE_RETALIATION_EXPECTED_DEFENSES = ["retaliatory_eviction", "habitability_defense"]
FIXTURE_RETALIATION_EXPECTED_STRENGTH = "strong"


# ---------------------------------------------------------------------------
# Fixture 4: SECTION 8 PROCEDURAL VIOLATION
# ---------------------------------------------------------------------------
FIXTURE_SECTION8 = {
    "case_number": "PA-MDJ-2024-000004",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "court_name": "Magisterial District Court 03-1-05",
    "landlord_name": "Harbor View Properties LP",
    "landlord_entity_type": "LLC",
    "landlord_address": "400 Harbor Blvd, Philadelphia, PA 19148",
    "tenant_name": "Nguyen Van Thanh",
    "tenant_phone": "+12155550404",
    "property_address": "25 Spring Garden St Apt 2B, Philadelphia, PA 19123",
    "zip_code": "19123",
    "filing_date": "2024-03-20",
    "filing_reason": "Breach of lease terms — unauthorized occupants",
    "claimed_amount": None,
    "notice_date": "2024-03-05",
    "notice_type": "cure_or_quit",
    "notice_period_days": 15,
    "hearing_date": "2024-04-15",
    "document_uri": "gs://evictionshield-test/fixtures/case_000004.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-03-21T08:00:00Z",
    # DEFECT: No mention of HUD notification for Section 8 tenant
    # No reference to Housing Authority notification in filing
    "_test_tenant_is_section8": True,
    "_test_hud_notification_found": False,
}

FIXTURE_SECTION8_EXPECTED_DEFENSES = ["section8_procedural_violation"]
FIXTURE_SECTION8_EXPECTED_CONFIDENCE = "medium"


# ---------------------------------------------------------------------------
# Fixture 5: IMPROPER PLAINTIFF — entity mismatch
# ---------------------------------------------------------------------------
FIXTURE_ENTITY_MISMATCH = {
    "case_number": "PA-MDJ-2024-000005",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "court_name": "Magisterial District Court 05-3-15",
    "landlord_name": "John Doe",         # FILER: individual name
    "landlord_entity_type": "individual",
    "landlord_address": "50 Walnut St, Philadelphia, PA 19106",
    "tenant_name": "Patricia Williams",
    "tenant_phone": "+12155550505",
    "property_address": "900 Pine St Apt 4A, Philadelphia, PA 19107",
    "zip_code": "19107",
    "filing_date": "2024-07-01",
    "filing_reason": "Nonpayment of rent — June 2024, $1,500.00",
    "claimed_amount": 1500.00,
    "notice_date": "2024-06-19",
    "notice_type": "pay_or_quit",
    "notice_period_days": 12,
    "hearing_date": "2024-07-25",
    "document_uri": "gs://evictionshield-test/fixtures/case_000005.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-07-02T08:00:00Z",
    # DEFECT: Lease was with "Pine Street Holdings LLC" but individual "John Doe" filed
    "_test_lease_counterparty": "Pine Street Holdings LLC",
    "_test_filer_name": "John Doe",
    "_test_entity_type_on_lease": "LLC",
    "_test_entity_type_of_filer": "individual",
}

FIXTURE_ENTITY_MISMATCH_EXPECTED_DEFENSES = ["improper_plaintiff"]
FIXTURE_ENTITY_MISMATCH_EXPECTED_CONFIDENCE = "high"


# ---------------------------------------------------------------------------
# Fixtures 6–20: Additional test cases covering remaining defense types
# ---------------------------------------------------------------------------

FIXTURE_DEFECTIVE_DELIVERY = {
    "case_number": "PA-MDJ-2024-000006",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "landlord_name": "Quick Flip Rentals LLC",
    "landlord_entity_type": "LLC",
    "tenant_name": "David Chen",
    "tenant_phone": "+12155550606",
    "property_address": "110 South St, Philadelphia, PA 19147",
    "zip_code": "19147",
    "filing_date": "2024-05-20",
    "notice_date": "2024-05-06",
    "notice_type": "pay_or_quit",
    "notice_period_days": 14,
    "filing_reason": "Nonpayment of rent",
    "claimed_amount": 800.00,
    "hearing_date": "2024-06-15",
    "stated_delivery_method": "email",  # DEFECT: Email not a valid PA delivery method
    "document_uri": "gs://evictionshield-test/fixtures/case_000006.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-05-21T08:00:00Z",
}
FIXTURE_DEFECTIVE_DELIVERY_EXPECTED_DEFENSES = ["defective_notice_delivery"]

FIXTURE_WAIVER = {
    "case_number": "PA-MDJ-2024-000007",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "landlord_name": "Midtown Apartments Inc",
    "landlord_entity_type": "corporation",
    "tenant_name": "Angela Davis",
    "tenant_phone": "+12155550707",
    "property_address": "300 Chestnut St Apt 7F, Philadelphia, PA 19106",
    "zip_code": "19106",
    "filing_date": "2024-06-10",
    "notice_date": "2024-05-20",
    "notice_type": "pay_or_quit",
    "notice_period_days": 21,
    "filing_reason": "Nonpayment of rent",
    "claimed_amount": 1100.00,
    "hearing_date": "2024-07-05",
    "document_uri": "gs://evictionshield-test/fixtures/case_000007.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-06-11T08:00:00Z",
    "_test_payment_after_notice": {"date": "2024-05-28", "amount": 550.00},  # DEFECT: waiver
}
FIXTURE_WAIVER_EXPECTED_DEFENSES = ["waiver_by_rent_acceptance"]

FIXTURE_HABITABILITY = {
    "case_number": "PA-MDJ-2024-000008",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "landlord_name": "Neglected Units LLC",
    "landlord_entity_type": "LLC",
    "tenant_name": "James Brown",
    "tenant_phone": "+12155550808",
    "property_address": "45 Germantown Ave, Philadelphia, PA 19144",
    "zip_code": "19144",
    "filing_date": "2024-04-05",
    "notice_date": "2024-03-23",
    "notice_type": "pay_or_quit",
    "notice_period_days": 13,
    "filing_reason": "Nonpayment of rent — tenant withheld rent citing uninhabitable conditions",
    "claimed_amount": 900.00,
    "hearing_date": "2024-05-01",
    "document_uri": "gs://evictionshield-test/fixtures/case_000008.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-04-06T08:00:00Z",
    "_test_landlord_profile": {
        "habitability_complaint_count": 5,
        "complaint_to_filing_correlation": 0.45,
    },
}
FIXTURE_HABITABILITY_EXPECTED_DEFENSES = ["habitability_defense"]

FIXTURE_LICENSURE = {
    "case_number": "PA-MDJ-2024-000009",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "landlord_name": "Unlicensed Rentals LLC",
    "landlord_entity_type": "LLC",
    "tenant_name": "Sarah Connor",
    "tenant_phone": "+12155550909",
    "property_address": "2000 Frankford Ave Apt 3, Philadelphia, PA 19125",
    "zip_code": "19125",
    "filing_date": "2024-05-12",
    "notice_date": "2024-04-27",
    "notice_type": "pay_or_quit",
    "notice_period_days": 15,
    "filing_reason": "Nonpayment of rent",
    "claimed_amount": 750.00,
    "hearing_date": "2024-06-08",
    "document_uri": "gs://evictionshield-test/fixtures/case_000009.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-05-13T08:00:00Z",
    "_test_rental_license_valid": False,   # DEFECT: license expired
}
FIXTURE_LICENSURE_EXPECTED_DEFENSES = ["landlord_licensure_deficiency"]

FIXTURE_MISSING_DISCLOSURES = {
    "case_number": "PA-MDJ-2024-000010",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "landlord_name": "Incomplete Filings Corp",
    "landlord_entity_type": "corporation",
    "tenant_name": "Thomas Wright",
    "tenant_phone": "+12155551010",
    "property_address": "88 Lehigh Ave, Philadelphia, PA 19133",
    "zip_code": "19133",
    "filing_date": "2024-06-18",
    "notice_date": None,        # DEFECT: No notice date disclosed
    "notice_type": None,        # DEFECT: No notice type
    "notice_period_days": None,
    "filing_reason": "Possession — tenant over-holding",
    "claimed_amount": None,
    "hearing_date": "2024-07-12",
    "document_uri": "gs://evictionshield-test/fixtures/case_000010.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-06-19T08:00:00Z",
    "_test_missing_disclosures": ["notice_date", "notice_type", "delivery_method"],
}
FIXTURE_MISSING_DISCLOSURES_EXPECTED_DEFENSES = ["missing_required_disclosures"]

FIXTURE_DISCRIMINATORY = {
    "case_number": "PA-MDJ-2024-000011",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "landlord_name": "Volume Evictions LLC",
    "landlord_entity_type": "LLC",
    "tenant_name": "Marcus Johnson",
    "tenant_phone": "+12155551111",
    "property_address": "5000 N Broad St Apt 2A, Philadelphia, PA 19141",
    "zip_code": "19141",
    "filing_date": "2024-03-05",
    "notice_date": "2024-02-20",
    "notice_type": "pay_or_quit",
    "notice_period_days": 14,
    "filing_reason": "Nonpayment of rent",
    "claimed_amount": 850.00,
    "hearing_date": "2024-04-01",
    "document_uri": "gs://evictionshield-test/fixtures/case_000011.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-03-06T08:00:00Z",
    "_test_landlord_profile": {
        "filing_count_90_days": 35,
        "protected_class_filing_pct": 0.82,  # DEFECT: 82% of filings against protected class
        "pattern_flag": "fair_housing_concern",
    },
}
FIXTURE_DISCRIMINATORY_EXPECTED_DEFENSES = ["discriminatory_filing_pattern"]

FIXTURE_MORATORIUM = {
    "case_number": "PA-MDJ-2024-000012",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "landlord_name": "COVID Era Filings LLC",
    "landlord_entity_type": "LLC",
    "tenant_name": "Linda Martinez",
    "tenant_phone": "+12155551212",
    "property_address": "1 Penn Square, Philadelphia, PA 19107",
    "zip_code": "19107",
    "filing_date": "2021-06-15",   # Filed during PA COVID moratorium
    "notice_date": "2021-06-01",
    "notice_type": "pay_or_quit",
    "notice_period_days": 14,
    "filing_reason": "Nonpayment of rent — COVID period",
    "claimed_amount": 1800.00,
    "hearing_date": "2021-07-10",
    "document_uri": "gs://evictionshield-test/fixtures/case_000012.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2021-06-16T08:00:00Z",
}
FIXTURE_MORATORIUM_EXPECTED_DEFENSES = ["moratorium_applicability"]

FIXTURE_SUMMONS_DEFECT = {
    "case_number": "PA-MDJ-2024-000013",
    "jurisdiction_code": "PA-UJS",
    "state": "PA",
    "landlord_name": "Procedural Errors Corp",
    "landlord_entity_type": "corporation",
    "tenant_name": "Kevin Park",
    "tenant_phone": "+12155551313",
    "property_address": "300 E Girard Ave Apt 1, Philadelphia, PA 19125",
    "zip_code": "19125",
    "filing_date": "2024-06-20",
    "notice_date": "2024-06-05",
    "notice_type": "pay_or_quit",
    "notice_period_days": 15,
    "filing_reason": "Nonpayment of rent",
    "claimed_amount": 1050.00,
    "hearing_date": "2024-06-25",   # DEFECT: Hearing 5 days after filing — MDJ requires ≥10 days notice
    "document_uri": "gs://evictionshield-test/fixtures/case_000013.pdf",
    "document_format": "pdf",
    "ingestion_timestamp": "2024-06-21T08:00:00Z",
    "_test_hearing_days_after_filing": 5,  # < required 10 days
}
FIXTURE_SUMMONS_DEFECT_EXPECTED_DEFENSES = ["procedural_defect_summons"]

# All 20 fixtures as a list for parameterized tests
ALL_FIXTURES = [
    {"id": "clean", "data": FIXTURE_CLEAN, "expected_defenses": FIXTURE_CLEAN_EXPECTED_DEFENSES},
    {"id": "improper_notice", "data": FIXTURE_IMPROPER_NOTICE, "expected_defenses": FIXTURE_IMPROPER_NOTICE_EXPECTED_DEFENSES},
    {"id": "retaliation", "data": FIXTURE_RETALIATION, "expected_defenses": FIXTURE_RETALIATION_EXPECTED_DEFENSES},
    {"id": "section8", "data": FIXTURE_SECTION8, "expected_defenses": FIXTURE_SECTION8_EXPECTED_DEFENSES},
    {"id": "entity_mismatch", "data": FIXTURE_ENTITY_MISMATCH, "expected_defenses": FIXTURE_ENTITY_MISMATCH_EXPECTED_DEFENSES},
    {"id": "defective_delivery", "data": FIXTURE_DEFECTIVE_DELIVERY, "expected_defenses": FIXTURE_DEFECTIVE_DELIVERY_EXPECTED_DEFENSES},
    {"id": "waiver", "data": FIXTURE_WAIVER, "expected_defenses": FIXTURE_WAIVER_EXPECTED_DEFENSES},
    {"id": "habitability", "data": FIXTURE_HABITABILITY, "expected_defenses": FIXTURE_HABITABILITY_EXPECTED_DEFENSES},
    {"id": "licensure", "data": FIXTURE_LICENSURE, "expected_defenses": FIXTURE_LICENSURE_EXPECTED_DEFENSES},
    {"id": "missing_disclosures", "data": FIXTURE_MISSING_DISCLOSURES, "expected_defenses": FIXTURE_MISSING_DISCLOSURES_EXPECTED_DEFENSES},
    {"id": "discriminatory", "data": FIXTURE_DISCRIMINATORY, "expected_defenses": FIXTURE_DISCRIMINATORY_EXPECTED_DEFENSES},
    {"id": "moratorium", "data": FIXTURE_MORATORIUM, "expected_defenses": FIXTURE_MORATORIUM_EXPECTED_DEFENSES},
    {"id": "summons_defect", "data": FIXTURE_SUMMONS_DEFECT, "expected_defenses": FIXTURE_SUMMONS_DEFECT_EXPECTED_DEFENSES},
    # Fixtures 14-20: Multi-defense combinations
    {"id": "multi_notice_habitability", "data": {**FIXTURE_IMPROPER_NOTICE, "case_number": "PA-MDJ-2024-000014", **FIXTURE_HABITABILITY.get("_test_landlord_profile", {})}, "expected_defenses": ["improper_notice_period", "habitability_defense"]},
    {"id": "multi_retaliation_waiver", "data": {**FIXTURE_RETALIATION, "case_number": "PA-MDJ-2024-000015", **FIXTURE_WAIVER.get("_test_payment_after_notice", {})}, "expected_defenses": ["retaliatory_eviction", "waiver_by_rent_acceptance"]},
    {"id": "section8_licensure", "data": {**FIXTURE_SECTION8, "case_number": "PA-MDJ-2024-000016"}, "expected_defenses": ["section8_procedural_violation"]},
    {"id": "entity_missing_disclosures", "data": {**FIXTURE_ENTITY_MISMATCH, "case_number": "PA-MDJ-2024-000017"}, "expected_defenses": ["improper_plaintiff"]},
    {"id": "high_volume_discriminatory", "data": {**FIXTURE_DISCRIMINATORY, "case_number": "PA-MDJ-2024-000018"}, "expected_defenses": ["discriminatory_filing_pattern"]},
    {"id": "defective_summons_notice", "data": {**FIXTURE_SUMMONS_DEFECT, "case_number": "PA-MDJ-2024-000019"}, "expected_defenses": ["procedural_defect_summons"]},
    {"id": "complex_multi_defense", "data": {**FIXTURE_RETALIATION, "case_number": "PA-MDJ-2024-000020", **{"notice_period_days": 6, "notice_date": "2024-04-04"}}, "expected_defenses": ["improper_notice_period", "retaliatory_eviction", "habitability_defense"]},
]
