-- EvictionShield — Module 3A: BigQuery Schema DDL
-- Dataset: evictionshield
-- All tables use STRING primary keys (deterministic hashes) for idempotent upserts.

-- ============================================================
-- TABLE: landlord_profiles
-- One row per unique landlord (normalized name + state).
-- Updated by the Dataflow pipeline every 6 hours.
-- ============================================================

CREATE TABLE IF NOT EXISTS `evictionshield.landlord_profiles` (
    -- Identifiers
    landlord_id                     STRING      NOT NULL  -- SHA-256[:32] of normalized_name|state
        OPTIONS(description="Deterministic hash of normalized landlord name + state code"),
    landlord_name_normalized        STRING      NOT NULL
        OPTIONS(description="Uppercase trimmed name used for deduplication and display"),
    entity_type                     STRING
        OPTIONS(description="individual | LLC | corporation | property_mgmt_company | unknown"),
    state                           STRING      NOT NULL
        OPTIONS(description="Two-letter state code"),

    -- Filing volume
    total_filings                   INT64       NOT NULL  DEFAULT 0,
    filings_last_90_days            INT64       NOT NULL  DEFAULT 0,
    filings_last_365_days           INT64       NOT NULL  DEFAULT 0,

    -- Outcomes
    withdrawal_count                INT64       NOT NULL  DEFAULT 0,
    withdrawal_rate_pct             FLOAT64     NOT NULL  DEFAULT 0.0
        OPTIONS(description="withdrawal_count / total_filings * 100"),
    default_judgment_count          INT64       NOT NULL  DEFAULT 0,
    contested_case_count            INT64       NOT NULL  DEFAULT 0,
    tenant_win_rate_when_represented FLOAT64    NOT NULL  DEFAULT 0.0
        OPTIONS(description="% of contested cases where represented tenant prevailed"),

    -- Habitability
    habitability_complaint_count    INT64       NOT NULL  DEFAULT 0,
    complaint_to_filing_correlation FLOAT64     NOT NULL  DEFAULT 0.0
        OPTIONS(description="Pearson correlation: filings within 90 days of habitability complaint"),

    -- Fair Housing
    protected_class_filing_pct      FLOAT64     NOT NULL  DEFAULT 0.0
        OPTIONS(description="% of filings against tenants in identified protected-class demographics"),

    -- Procedural behavior
    avg_days_notice_given           FLOAT64
        OPTIONS(description="Average days between notice_date and filing_date across all filings"),
    procedural_defect_rate          FLOAT64     NOT NULL  DEFAULT 0.0
        OPTIONS(description="% of past filings with at least one identified procedural defect"),

    -- Metadata
    last_updated                    TIMESTAMP   NOT NULL
)
PARTITION BY DATE(last_updated)
CLUSTER BY state, entity_type
OPTIONS(
    description="One row per unique landlord. Updated by Dataflow pipeline every 6 hours.",
    require_partition_filter=false
);

-- ============================================================
-- TABLE: filing_events
-- One row per eviction filing. Immutable after insert except
-- for outcome fields populated post-hearing.
-- ============================================================

CREATE TABLE IF NOT EXISTS `evictionshield.filing_events` (
    -- Identifiers
    event_id                        STRING      NOT NULL
        OPTIONS(description="Deterministic hash of case_number + filing_date to ensure idempotency"),
    case_number                     STRING      NOT NULL,
    landlord_id                     STRING      NOT NULL
        OPTIONS(description="FK → landlord_profiles.landlord_id"),

    -- Filing details
    filing_date                     DATE        NOT NULL,
    jurisdiction                    STRING      NOT NULL,
    zip_code                        STRING,
    filing_reason                   STRING,

    -- Notice
    notice_date                     DATE,
    notice_period_days              INT64
        OPTIONS(description="Computed: DATE_DIFF(filing_date, notice_date, DAY)"),
    hearing_date                    DATE,

    -- Analysis results (written by Gemini analysis function)
    defenses_identified             JSON
        OPTIONS(description="Array of DefenseIdentified objects as JSON"),

    -- Outcome (populated post-hearing via POST /cases/{id}/outcome API)
    outcome                         STRING
        OPTIONS(description="won | lost | settled | dismissed | no_show | NULL if pending"),
    outcome_date                    DATE,
    represented                     BOOL
        OPTIONS(description="Was the tenant represented by an attorney at hearing?"),
    withdrawn                       BOOL        DEFAULT FALSE,
    withdrawn_date                  DATE
)
PARTITION BY filing_date
CLUSTER BY jurisdiction, landlord_id, zip_code
OPTIONS(
    description="One row per eviction filing. Outcomes backfilled from API.",
    require_partition_filter=false
);

-- ============================================================
-- TABLE: habitability_complaints
-- Sourced from city/county housing inspection databases.
-- Joined to landlord_profiles on property_address_normalized.
-- ============================================================

CREATE TABLE IF NOT EXISTS `evictionshield.habitability_complaints` (
    complaint_id                    STRING      NOT NULL
        OPTIONS(description="Source system complaint ID — prefixed with source code for uniqueness"),
    property_address_normalized     STRING      NOT NULL
        OPTIONS(description="Normalized address: uppercase, no punctuation, abbreviated street suffix"),
    zip_code                        STRING,
    complaint_date                  DATE        NOT NULL,
    complaint_category              STRING
        OPTIONS(description="HVAC | plumbing | pest | mold | structural | electrical | lead | other"),
    severity                        STRING
        OPTIONS(description="critical | major | minor | informational"),
    resolution_status               STRING
        OPTIONS(description="open | resolved | pending_reinspection | closed_no_action"),
    resolution_date                 DATE,
    landlord_id                     STRING
        OPTIONS(description="FK → landlord_profiles.landlord_id — nullable, joined after ingestion"),
    source_database                 STRING
        OPTIONS(description="Origin: 311_phila | la_housing | nyc_hpd | chicago_311 | etc.")
)
PARTITION BY complaint_date
CLUSTER BY zip_code, landlord_id, complaint_category
OPTIONS(
    description="Housing inspection complaints. Joined by address to landlord_profiles.",
    require_partition_filter=false
);

-- ============================================================
-- TABLE: defense_effectiveness
-- Aggregate outcome statistics per defense type + jurisdiction.
-- Updated by the outcome ingestion Cloud Function.
-- Used by the BigQuery ML model for training features.
-- ============================================================

CREATE TABLE IF NOT EXISTS `evictionshield.defense_effectiveness` (
    defense_type                    STRING      NOT NULL,
    jurisdiction                    STRING      NOT NULL,
    judge_id_anon                   STRING
        OPTIONS(description="Anonymized judge identifier for per-judge analysis"),
    landlord_entity_type            STRING,
    tenant_represented              BOOL        NOT NULL,
    total_cases                     INT64       NOT NULL  DEFAULT 0,
    cases_defense_succeeded         INT64       NOT NULL  DEFAULT 0,
    defense_success_rate            FLOAT64     NOT NULL  DEFAULT 0.0
        OPTIONS(description="cases_defense_succeeded / total_cases"),
    sample_size                     INT64       NOT NULL  DEFAULT 0
        OPTIONS(description="Same as total_cases — surfaced for BQML feature"),
    last_updated                    TIMESTAMP   NOT NULL
)
PARTITION BY DATE(last_updated)
CLUSTER BY jurisdiction, defense_type
OPTIONS(description="Rolling defense effectiveness stats for BQML training.");


-- ============================================================
-- VIEW: landlord_risk_scores
-- Composite risk score (0-100) derived from behavioral signals.
-- Consumed by the Gemini analysis prompt and the dashboard API.
-- ============================================================

CREATE OR REPLACE VIEW `evictionshield.landlord_risk_scores` AS
WITH
complaint_summary AS (
    SELECT
        landlord_id,
        COUNT(*) AS total_complaints,
        COUNTIF(resolution_status IN ('open', 'pending_reinspection')) AS unresolved_complaints,
        COUNTIF(severity = 'critical') AS critical_complaints
    FROM `evictionshield.habitability_complaints`
    WHERE landlord_id IS NOT NULL
    GROUP BY landlord_id
),
scores AS (
    SELECT
        lp.landlord_id,
        lp.landlord_name_normalized,
        lp.entity_type,
        lp.state,
        lp.total_filings,
        lp.filings_last_90_days,
        lp.withdrawal_rate_pct,
        lp.habitability_complaint_count,
        lp.complaint_to_filing_correlation,
        lp.procedural_defect_rate,
        lp.protected_class_filing_pct,
        lp.tenant_win_rate_when_represented,
        cs.total_complaints,
        cs.unresolved_complaints,
        cs.critical_complaints,

        -- Composite risk score (0–100): higher = more concerning filing pattern
        LEAST(100.0,
            -- Volume component (0-25): high-volume filers get higher scores
            LEAST(25.0, lp.filings_last_90_days * 1.25)

            -- Withdrawal rate component (0-20): high withdrawal = filing as harassment
            + LEAST(20.0, lp.withdrawal_rate_pct * 0.20)

            -- Defect rate component (0-20): serial procedural violations
            + LEAST(20.0, lp.procedural_defect_rate * 20.0)

            -- Complaint correlation component (0-25): retaliation signal
            + LEAST(25.0, lp.complaint_to_filing_correlation * 25.0)

            -- Fair housing component (0-10): disproportionate protected-class filing
            + LEAST(10.0, lp.protected_class_filing_pct * 10.0)
        ) AS composite_risk_score,

        CASE
            WHEN lp.filings_last_90_days > 20 THEN 'high_volume_filer'
            WHEN lp.withdrawal_rate_pct > 40.0 THEN 'high_withdrawal_rate'
            WHEN lp.complaint_to_filing_correlation > 0.5 THEN 'retaliation_pattern'
            WHEN lp.protected_class_filing_pct > 0.6 THEN 'fair_housing_concern'
            WHEN lp.procedural_defect_rate > 0.3 THEN 'serial_procedural_violator'
            ELSE NULL
        END AS primary_risk_flag,

        lp.last_updated
    FROM `evictionshield.landlord_profiles` lp
    LEFT JOIN complaint_summary cs USING (landlord_id)
)
SELECT * FROM scores;


-- ============================================================
-- VIEW: zip_eviction_trends
-- Monthly eviction filing counts and withdrawal rates by ZIP.
-- Used in the dashboard analytics endpoint and policy reporting.
-- ============================================================

CREATE OR REPLACE VIEW `evictionshield.zip_eviction_trends` AS
SELECT
    zip_code,
    jurisdiction,
    DATE_TRUNC(filing_date, MONTH) AS filing_month,
    COUNT(*) AS total_filings,
    COUNTIF(withdrawn = TRUE) AS withdrawn_count,
    SAFE_DIVIDE(COUNTIF(withdrawn = TRUE), COUNT(*)) * 100 AS withdrawal_rate_pct,
    COUNTIF(outcome = 'dismissed') AS dismissed_count,
    COUNTIF(outcome = 'won') AS tenant_win_count,
    COUNTIF(represented = TRUE) AS represented_count,
    SAFE_DIVIDE(COUNTIF(represented = TRUE), COUNT(*)) * 100 AS representation_rate_pct,
    AVG(notice_period_days) AS avg_notice_period_days
FROM `evictionshield.filing_events`
WHERE filing_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 24 MONTH)
  AND zip_code IS NOT NULL
GROUP BY zip_code, jurisdiction, filing_month
ORDER BY filing_month DESC, total_filings DESC;
