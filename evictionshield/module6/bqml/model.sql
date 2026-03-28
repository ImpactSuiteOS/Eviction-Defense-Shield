-- EvictionShield — Module 6B: BigQuery ML Defense Effectiveness Model
-- Logistic regression predicting whether a raised defense will succeed.
-- Retrained weekly via Cloud Build job.

-- ============================================================
-- CREATE MODEL: defense_effectiveness_model
-- ============================================================

CREATE OR REPLACE MODEL `evictionshield.defense_effectiveness_model`
OPTIONS (
    model_type = 'LOGISTIC_REG',
    input_label_cols = ['defense_succeeded'],
    data_split_method = 'AUTO_SPLIT',
    max_iterations = 50,
    learn_rate_strategy = 'ADAPTIVE',
    auto_class_weights = TRUE,  -- Handle class imbalance (most defenses don't succeed without counsel)
    l2_reg = 0.1,
    model_registry = 'VERTEX_AI',  -- Register in Vertex AI Model Registry for versioning
    vertex_ai_model_id = 'defense-effectiveness-model',
    vertex_ai_model_version_aliases = ['latest']
)
AS
SELECT
    -- Label
    CAST(
        CASE
            WHEN de.cases_defense_succeeded > 0 AND fe.outcome IN ('won', 'dismissed') THEN TRUE
            ELSE FALSE
        END AS BOOL
    ) AS defense_succeeded,

    -- Features
    de.defense_type,
    de.jurisdiction,
    COALESCE(fe.represented, FALSE) AS tenant_represented,
    lp.entity_type AS landlord_entity_type,
    COALESCE(fe.notice_period_days, 0) AS days_notice_given,
    COALESCE(lp.filings_last_90_days, 0) AS landlord_filing_count_90_days,
    COALESCE(lp.habitability_complaint_count, 0) AS habitability_complaint_count,

    -- Anonymized judge ID (if available from court records — NULL-safe)
    COALESCE(fe.judge_id_anon, 'UNKNOWN') AS judge_id_anon

FROM `evictionshield.filing_events` fe
JOIN `evictionshield.landlord_profiles` lp
    ON fe.landlord_id = lp.landlord_id

-- Cross-join with defense_effectiveness to get per-defense labels
-- We explode the defenses_identified JSON array into rows
JOIN UNNEST(
    JSON_EXTRACT_ARRAY(fe.defenses_identified)
) AS defense_json

-- Parse defense type from the JSON element
JOIN `evictionshield.defense_effectiveness` de
    ON JSON_EXTRACT_SCALAR(defense_json, '$.defense_type') = de.defense_type
    AND fe.jurisdiction = de.jurisdiction

WHERE
    -- Only train on cases with known outcomes
    fe.outcome IS NOT NULL
    AND fe.outcome != 'no_show'
    -- Minimum sample size guard: at least 30 cases for this defense type
    AND de.total_cases >= 30
    -- Only cases filed in the past 3 years for relevance
    AND fe.filing_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 YEAR);


-- ============================================================
-- ML.PREDICT QUERY — used at inference time by the analysis engine
-- ============================================================
-- This query is called from the Python analysis engine with
-- @jurisdiction and @defense_types as parameters.

-- INFERENCE QUERY (reference — called programmatically in analysis_engine.py):
/*
SELECT
    defense_type,
    jurisdiction,
    ROUND(predicted_defense_succeeded_probs[OFFSET(1)].prob * 100, 1) AS success_probability_pct,
    sample_size
FROM
    ML.PREDICT(
        MODEL `evictionshield.defense_effectiveness_model`,
        (
            SELECT
                defense_type,
                jurisdiction,
                TRUE AS tenant_represented,       -- Assume represented (optimistic estimate)
                'LLC' AS landlord_entity_type,    -- Default if not known at predict time
                10 AS days_notice_given,
                5 AS landlord_filing_count_90_days,
                0 AS habitability_complaint_count,
                'UNKNOWN' AS judge_id_anon,
                sample_size
            FROM `evictionshield.defense_effectiveness`
            WHERE jurisdiction = 'PA-UJS'
              AND defense_type IN ('improper_notice_period', 'retaliatory_eviction')
        )
    )
*/


-- ============================================================
-- EVALUATE MODEL — run after each retraining cycle
-- ============================================================

-- SELECT * FROM ML.EVALUATE(MODEL `evictionshield.defense_effectiveness_model`);

-- Expected output reference values (validate after each retraining):
-- precision > 0.60, recall > 0.55, roc_auc > 0.70


-- ============================================================
-- FEATURE IMPORTANCE — run after retraining for explainability audit
-- ============================================================

-- SELECT * FROM ML.GLOBAL_EXPLAIN(MODEL `evictionshield.defense_effectiveness_model`);

-- Expected top features (ordered by importance):
-- 1. defense_type (categorical — strongest predictor)
-- 2. tenant_represented (boolean — large effect)
-- 3. jurisdiction (categorical)
-- 4. landlord_filing_count_90_days (numeric)
-- 5. days_notice_given (numeric)
