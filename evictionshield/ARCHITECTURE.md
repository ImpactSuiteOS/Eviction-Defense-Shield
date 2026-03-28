# EvictionShield — Complete Architecture, Cost Model, and Launch Plan

---

## Part 1: Complete Data Flow

Every event, service, and data store — in the exact order data moves through the system.

```
COURT FILING → ATTORNEY BRIEF IN UNDER 60 MINUTES
```

### Stage 1: Ingestion (Module 1A)

```
Court Website (UJS Portal)
    ↓ OAuth2 / HTTP
Cloud Run: evictionshield-ingestion
    ├── authenticate() → Secret Manager: court-api-creds-pa
    ├── fetch_new_filings(since_timestamp) → PA UJS REST API
    ├── download_document() → Court document binary (PDF/XML/JSON)
    ├── upload_to_gcs() → GCS: gs://[project]-raw-filings/pa/YYYY/MM/DD/[case].pdf
    └── publish(NormalizedFiling) → Pub/Sub: raw-eviction-filings
         └── Stored: Firestore: ingestion_checkpoints/{adapter_id}
```

### Stage 2: Document Processing (Module 1B)

```
Pub/Sub: raw-eviction-filings
    ↓ (push subscription)
Cloud Function: evictionshield-docprocessing
    ├── download_from_gcs() → GCS raw-filings bucket
    ├── Route by document type:
    │   ├── PDF → Document AI: Form Parser processor
    │   ├── Image → Document AI: OCR processor
    │   └── XML/JSON → Direct schema mapping (no AI)
    ├── Extract 13 fields with confidence scores
    ├── Confidence gate (threshold: 0.75):
    │   ├── PASS → Firestore: eviction_filings (set/merge on case_number)
    │   │          Pub/Sub: structured-filings-ready
    │   └── FAIL → Firestore: eviction_filings (partial)
    │              Pub/Sub: low-confidence-filings (human review queue)
    └── Cloud Logging: structured extraction event
```

### Stage 3: Gemini Legal Analysis (Module 2C)

```
Pub/Sub: structured-filings-ready
    ↓ (push subscription)
Cloud Function: evictionshield-analysis
    ├── Load JurisdictionRuleset → GCS: [project]-rulesets/rulesets/PA-UJS.yaml
    ├── Get landlord profile → BigQuery: evictionshield.landlord_profiles
    ├── Get defense probabilities → BigQuery ML: evictionshield.defense_effectiveness_model
    ├── Build system prompt (BASE + jurisdiction rules + output schema)
    ├── Build user prompt (filing data + landlord profile + BQML probabilities)
    ├── Vertex AI: Gemini 1.5 Pro (response_mime_type="application/json")
    ├── Pydantic validation → EvictionAnalysisResult
    │   └── On failure: retry with correction prompt (max 1 retry)
    ├── Write → Firestore: case_analyses/{case_number}
    └── Publish → Pub/Sub: case-analysis-complete
         └── Cloud Logging: analysis event with latency, defenses, routing
```

### Stage 4: Tenant Notification (Module 4A)

```
Firestore: case_analyses/{caseId} [NEW DOCUMENT TRIGGER]
    ↓ (Firestore event trigger, routing_recommendation = urgent/standard)
Cloud Function: evictionshield-sms-notify
    ├── Fetch tenant phone → Firestore: eviction_filings
    ├── Detect language → name heuristics + ZIP demographic map
    ├── Generate SMS → Vertex AI: Gemini 1.5 Flash (≤160 chars, 6th-grade level)
    ├── Create short link → https://app.evictionshield.io/intake?c=[case_number]
    ├── Enqueue delivery → Cloud Tasks: sms-notifications queue
    │   └── Cloud Tasks → Cloud Function: send-sms-worker
    │       └── Twilio REST API → Tenant's phone (SMS delivery)
    └── Log → Firestore: notification_events (phone hash, not raw number)
```

### Stage 5: Tenant Intake (Module 4B/4C)

```
Tenant clicks SMS link → https://app.evictionshield.io/intake
    OR
Tenant calls Twilio number → Cloud Run: evictionshield-voice

Web path:
    Vertex AI Agent Builder (Dialogflow CX) ←→ Webhook: evictionshield-webhook
        ├── Phase 1: Confirm case (fetch from Firestore case_analyses)
        ├── Phase 2: Defense intake (6 questions about notice, payment, complaints)
        ├── Phase 3: Evidence checklist (generated from intake + defenses)
        └── Phase 4: Legal aid referral (Firestore: legal_aid_directory by ZIP)
             └── If urgent: Callback request → Firestore: callback_requests

Voice path:
    Twilio → /voice/inbound → TwiML → WebSocket /voice/stream
        ├── Google Cloud STT (streaming, telephony model)
        ├── Dialogflow CX (detect_intent)
        ├── Google Cloud TTS (Neural2, language auto-detected)
        └── Log: Firestore: call_transcripts
```

### Stage 6: Legal Aid Triage (Module 5A)

```
Legal Aid Attorney → Browser → https://dashboard.evictionshield.io
    ↓ HTTPS
Cloud Run: evictionshield-dashboard-api (FastAPI)
    ├── Firebase Auth token verification → JWT decode
    ├── Load org coverage → Firestore: legal_aid_orgs/{org_id}
    │
    ├── GET /cases
    │   └── Firestore: case_analyses WHERE zip_code IN [org_coverage_zips]
    │
    ├── GET /cases/{case_number}
    │   ├── Firestore: case_analyses
    │   ├── Firestore: eviction_filings
    │   ├── Firestore: tenant_intake_responses
    │   └── GCS signed URL → gs://[project]-attorney-briefs/briefs/[case]/brief.pdf
    │
    ├── POST /cases/{case_number}/assign
    │   ├── Verify attorney is in org → Firestore: user_profiles
    │   ├── Update → Firestore: case_analyses (assigned_attorney_uid)
    │   └── Pub/Sub: case-assigned → notification to attorney
    │
    └── POST /cases/{case_number}/outcome
        └── Write → Firestore: case_outcomes → [TRIGGERS MODULE 6A]
```

### Stage 7: Attorney Brief Generation (Module 5B)

```
Firestore: case_analyses [UPDATE: assigned_attorney_uid SET]
    ↓ (Firestore event trigger)
Cloud Function: evictionshield-brief-generator
    ├── Fetch: Firestore case_analyses + eviction_filings + tenant_intake_responses
    ├── Vertex AI: Gemini 1.5 Pro → structured brief content JSON
    ├── reportlab → PDF (letter size, legal formatting, Bluebook citations)
    ├── Upload → GCS: gs://[project]-attorney-briefs/briefs/[case]/brief.pdf
    └── Update → Firestore: case_analyses.attorney_brief_url (signed URL)
```

### Stage 8: Outcome Feedback Loop (Module 6A)

```
Firestore: case_outcomes [NEW DOCUMENT TRIGGER]
    ↓
Cloud Function: evictionshield-outcome-ingest
    ├── BigQuery MERGE → evictionshield.filing_events (outcome, represented, outcome_date)
    ├── BigQuery MERGE → evictionshield.defense_effectiveness
    │   └── Increment total_cases, cases_defense_succeeded per defense type
    ├── Create if needed → Firestore: analysis_reviews
    │   └── Condition: high-confidence defense identified BUT tenant lost
    └── Publish → Pub/Sub: trigger-profile-update (landlord_id)
         └── Dataflow or Cloud Function: recalculate landlord_profiles
```

### Stage 9: Landlord Profile Updates (Module 3B)

```
Cloud Scheduler (every 6 hours)
    ↓ HTTP → Dataflow REST API
Dataflow: landlord-profile-update (Apache Beam)
    ├── ReadFromBigQuery: evictionshield.filing_events
    ├── NormalizeFilingEventFn → (landlord_id, normalized_row)
    ├── GroupByKey → all events per landlord
    ├── ComputeLandlordMetricsFn → rolling 90d/365d counts, rates, defect rates
    ├── ReadFromBigQuery: evictionshield.habitability_complaints (side input)
    ├── ComputeComplaintCorrelationFn → complaint_to_filing_correlation
    └── WriteToBigQuery → evictionshield.landlord_profiles (WRITE_TRUNCATE)
```

### Stage 10: BQML Retraining (Module 6B)

```
Cloud Scheduler (Sundays 2 AM UTC)
    ↓ HTTP
Cloud Build: bqml-weekly-retrain
    ├── Step 1: Validate training data volume (>500 rows required)
    ├── Step 2: CREATE OR REPLACE MODEL evictionshield.defense_effectiveness_model
    ├── Step 3: ML.EVALUATE → quality gate (precision>0.55, roc_auc>0.65)
    ├── Step 4: Archive snapshot
    └── Step 5: Log retraining event to BigQuery
```

### Stage 11: Data Retention (Module 7C)

```
Cloud Scheduler (daily 2 AM UTC)
    ↓ HTTP
Cloud Function: evictionshield-retention-job
    ├── Query Firestore: eviction_filings WHERE pii_anonymized=false AND age>90d
    ├── Batch update: replace tenant_name/phone/email with ANON_ tokens
    ├── BigQuery verification: confirm no PII columns in filing_events schema
    └── Monthly (1st of month): compliance report → GCS: [project]-compliance/
```

---

## Part 2: Cost Estimates

### Cost Methodology

All estimates use 2024 GCP/Twilio list prices. Actual costs will vary based on
Document AI processor type, Gemini token counts, and Twilio messaging volume.

Gemini pricing assumes:
- Gemini 1.5 Pro: $3.50/1M input tokens, $10.50/1M output tokens
- Gemini 1.5 Flash: $0.075/1M input tokens, $0.30/1M output tokens
- Average filing analysis: ~3,000 input tokens + ~1,500 output tokens

Document AI pricing: $1.50/1,000 pages (Form Parser)

---

### Scale 1: 1,000 Filings/Month (Single City Pilot — Philadelphia)

| Category | Component | Monthly Cost |
|----------|-----------|-------------|
| **Compute** | Cloud Run (ingestion, voice, dashboard API) — min instances | $45 |
| | Cloud Functions (doc processing, analysis, notifications, outcome) | $15 |
| **AI Inference** | Document AI Form Parser: 1,000 pages × $1.50/1K | $1.50 |
| | Gemini 1.5 Pro analysis: 1,000 × (3K+1.5K tokens) = 4.5M tokens | $22 |
| | Gemini 1.5 Flash SMS: 1,000 × 500 tokens | $0.05 |
| | Google Cloud TTS (voice calls ~200/mo): 200 × 1K chars × $0.016/1K | $3.20 |
| | Google Cloud STT (voice): 200 × 3 min × $0.016/min | $9.60 |
| **Storage** | Firestore: ~50K reads/writes/day | $12 |
| | BigQuery: storage 1GB + queries 10GB processed | $5 |
| | Cloud Storage: 1GB raw filings + briefs | $0.50 |
| **Messaging** | Pub/Sub: 1,000 messages | $0.05 |
| | Twilio SMS: 1,000 messages × $0.0079 | $7.90 |
| **Other** | Secret Manager, Cloud Logging, Scheduler | $5 |
| **TOTAL** | | **~$126/month** |

**Revenue needed for sustainability at this scale:** $500–$2,000/month (1 legal aid org subscriber)

---

### Scale 2: 50,000 Filings/Month (10-City Deployment)

| Category | Component | Monthly Cost |
|----------|-----------|-------------|
| **Compute** | Cloud Run (3 services, min 2 instances each) | $800 |
| | Cloud Functions (invocations at scale) | $120 |
| | Dataflow (6-hour runs, 4 workers, n1-standard-4) | $450 |
| **AI Inference** | Document AI: 50,000 pages × $1.50/1K | $75 |
| | Gemini 1.5 Pro analysis: 50,000 × 4.5K tokens | $1,100 |
| | Gemini 1.5 Flash SMS: 50,000 × 500 tokens | $2.25 |
| | Gemini 1.5 Pro briefs: 10,000 briefs × 6K tokens | $630 |
| | Google Cloud TTS + STT (10,000 voice calls) | $640 |
| **Storage** | Firestore: 2.5M reads/writes/day | $600 |
| | BigQuery: 50GB storage + 500GB queries | $85 |
| | Cloud Storage: 50GB | $10 |
| **Messaging** | Pub/Sub: 50,000 messages | $2.50 |
| | Twilio SMS: 50,000 × $0.0079 | $395 |
| **Other** | BQML retraining (weekly), Cloud Build, monitoring | $150 |
| **TOTAL** | | **~$5,060/month** |

**Revenue at this scale:** $25,000–$50,000/month (10 legal aid orgs at $2,500–$5,000/mo each)
**Gross margin:** ~80–90% at this scale

---

### Scale 3: 500,000 Filings/Month (National Scale — ~15% of US evictions)

| Category | Component | Monthly Cost |
|----------|-----------|-------------|
| **Compute** | Cloud Run (auto-scale, committed use discounts) | $6,500 |
| | Cloud Functions | $800 |
| | Dataflow (continuous streaming, autoscaled) | $4,200 |
| **AI Inference** | Document AI: 500,000 pages × $1.50/1K | $750 |
| | Gemini 1.5 Pro analysis: 500K × 4.5K tokens | $11,000 |
| | Gemini 1.5 Flash SMS: 500K × 500 tokens | $22.50 |
| | Gemini 1.5 Pro briefs: 100K × 6K tokens | $6,300 |
| | Cloud TTS + STT (100K voice calls) | $6,400 |
| **Storage** | Firestore: 25M reads/writes/day | $5,800 |
| | BigQuery: 500GB storage + 5TB queries | $840 |
| | Cloud Storage: 500GB | $100 |
| **Messaging** | Pub/Sub: 500,000 messages | $25 |
| | Twilio SMS: 500K × $0.0075 (volume discount) | $3,750 |
| **Other** | Multi-region redundancy, VPC SC, monitoring | $3,000 |
| **TOTAL** | | **~$49,500/month** |

**Revenue at national scale:** $500,000–$1,000,000/month
**Gross margin:** ~90-95% at scale (AI costs are sublinear with volume discounts)

**Note:** At 500K/month scale, negotiate committed use discounts on Cloud Run and
Vertex AI. Google for Nonprofits credits may apply for legal aid partner organizations.

---

## Part 3: 90-Day Launch Sequence

### Phase 1: Days 1–21 — Foundation (Go/No-Go: Extraction Works)

**Goal:** Get one real filing from PA UJS into Firestore with correct fields.

| Day | Task |
|-----|------|
| 1–3 | GCP project setup: enable APIs, create service accounts, configure Secret Manager with PA UJS credentials |
| 3–5 | Terraform apply: GCS buckets, Pub/Sub topics, Firestore, BigQuery dataset |
| 5–8 | Deploy Module 1A: PA UJS adapter and ingestion Cloud Run service |
| 8–12 | Register Document AI Form Parser processor; train on 20 sample PA MDC filings |
| 12–16 | Deploy Module 1B: Document processing Cloud Function; test end-to-end with synthetic PDFs |
| 16–19 | Upload PA-UJS.yaml ruleset to GCS; validate against JurisdictionRuleset.validate_ruleset() |
| 19–21 | End-to-end smoke test: trigger ingestion, verify Firestore document with all 13 fields |

**Go/No-Go Criteria:**
- ✅ At least 1 real PA court filing ingested and extracted with ≥0.75 confidence on all critical fields
- ✅ Module 8 ingestion tests passing at 85%+ coverage
- ❌ STOP if: PA UJS API access denied, Document AI confidence consistently below 0.65

---

### Phase 2: Days 22–42 — Intelligence Layer (Go/No-Go: Analysis Works)

**Goal:** Get a valid EvictionAnalysisResult for the test case that identifies at least one defense correctly.

| Day | Task |
|-----|------|
| 22–25 | Deploy Module 2C: Gemini analysis Cloud Function; test with FIXTURE_IMPROPER_NOTICE |
| 25–28 | Populate BigQuery with 50 synthetic filing_events and 10 landlord profiles for testing |
| 28–30 | Validate Pydantic schema on 20 fixture cases; verify disclaimer always present |
| 30–33 | Add CA-COURTS.yaml ruleset; validate JurisdictionRuleset for California |
| 33–36 | Module 8 analysis tests passing; Gemini retry logic exercised |
| 36–40 | Manual legal review of 20 AI analyses by pro bono attorney → adjust system prompt |
| 40–42 | Benchmark: run all 20 fixtures, verify ≥80% defense identification accuracy |

**Go/No-Go Criteria:**
- ✅ Gemini correctly identifies expected defense in ≥16/20 test fixtures (80%)
- ✅ Zero hallucinated statute citations (verified by legal reviewer)
- ✅ All 20 fixture analyses complete in <30 seconds each
- ❌ STOP if: Gemini consistently misidentifies the improper_notice_period fixture

---

### Phase 3: Days 43–56 — Tenant Interface (Go/No-Go: Tenant Can Reach Help)

**Goal:** A test tenant receives an SMS within 5 minutes of filing ingestion and can reach a legal aid org.

| Day | Task |
|-----|------|
| 43–46 | Deploy Module 4A: SMS notifications; test all 4 languages with real Twilio sandbox |
| 46–49 | Deploy Module 4B: Dialogflow CX agent; test all 4 conversation phases with scripted inputs |
| 49–52 | Populate Firestore legal_aid_directory with 5 Philadelphia-area legal aid orgs |
| 52–54 | Deploy Module 4C: Voice interface; test with recorded audio clips |
| 54–56 | End-to-end test: filing → analysis → SMS → click link → complete intake → see legal aid orgs |

**Go/No-Go Criteria:**
- ✅ SMS received within 5 minutes of case analysis completing
- ✅ Dialogflow agent completes all 4 phases without looping or getting stuck
- ✅ Legal aid organization lookup returns results for Philadelphia ZIP codes
- ❌ STOP if: Twilio delivery rate below 85% in sandbox testing

---

### Phase 4: Days 57–70 — Legal Aid Dashboard (Go/No-Go: First Paying Customer Possible)

**Goal:** A legal aid attorney can log in, see cases, assign them, and record outcomes.

| Day | Task |
|-----|------|
| 57–60 | Deploy Module 5A: Dashboard API; configure Firebase Auth for first pilot org |
| 60–63 | Deploy Module 5B: Brief generator; generate 5 sample briefs for legal review |
| 63–65 | Configure Firestore security rules; verify org isolation with two test accounts |
| 65–67 | Module 8 API tests passing; authentication and data isolation verified |
| 67–70 | Onboard first legal aid pilot organization with 2 attorneys; collect feedback |

**Go/No-Go Criteria:**
- ✅ Attorney can log in, see only their org's cases, assign a case, download brief
- ✅ Brief PDF is legally accurate (verified by pilot attorney)
- ✅ Org isolation verified: org A cannot read org B's cases
- ❌ STOP if: First pilot attorney says brief is misleading or cites incorrect statutes

---

### Phase 5: Days 71–84 — Feedback Loop and Hardening (Go/No-Go: Production Ready)

**Goal:** System improves with outcomes data; security audit passed; 99% uptime achieved.

| Day | Task |
|-----|------|
| 71–74 | Deploy Module 6A: Outcome ingestion; record 10 real outcomes from pilot |
| 74–76 | Deploy Module 6B: BQML model (first training run on synthetic + pilot data) |
| 76–78 | Deploy Module 7C: Data retention job; verify PII anonymization works |
| 78–80 | Apply Firestore security rules to production; penetration test key endpoints |
| 80–82 | Load test dashboard API to 100 concurrent users; verify auto-scaling |
| 82–84 | Set up Cloud Monitoring alerts: Gemini latency P99 >30s, extraction confidence <0.70, SMS delivery rate <90% |

**Go/No-Go Criteria:**
- ✅ Outcome data flows from API → BigQuery → defense_effectiveness table
- ✅ BQML model trains without error (quality gate may not pass yet — that's OK)
- ✅ Load test: dashboard API handles 100 concurrent requests at <2s P95
- ✅ No critical security findings from penetration test
- ❌ STOP if: PII anonymization fails to run or leaves raw PII after 90-day window

---

### Phase 6: Day 85–90 — First Customer and Commercial Launch

| Day | Task |
|-----|------|
| 85 | Invoice pilot legal aid organization for month 1 ($2,500–$5,000) |
| 86 | Send press release to local housing justice organizations |
| 87–88 | Onboard second legal aid organization; expand to second city (test CA ruleset) |
| 89–90 | Launch public intake portal (app.evictionshield.io) with real case routing |

**First Revenue Milestone:** Day 85
**First Non-Paying User Impact:** Day 21 (first real filing analyzed)
**First Tenant Reached:** Day 56 (first real SMS sent to a real tenant)

---

## Architecture Summary: Service Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                    EvictionShield Production Services                │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  INGESTION LAYER                                                     │
│  ┌─────────────────────┐                                            │
│  │ Cloud Run            │ ← Court APIs (PA, CA, ...)                │
│  │ evictionshield-      │ → GCS raw-filings                         │
│  │ ingestion           │ → Pub/Sub: raw-eviction-filings            │
│  └─────────────────────┘                                            │
│                                                                       │
│  PROCESSING LAYER                                                    │
│  ┌─────────────────────┐    ┌──────────────────────────────┐       │
│  │ Cloud Function       │    │ Cloud Function                │       │
│  │ docprocessing       │    │ gemini-analysis               │       │
│  │ (Document AI)       │ → │ (Vertex AI: Gemini 1.5 Pro)   │       │
│  └─────────────────────┘    └──────────────────────────────┘       │
│                                                                       │
│  TENANT LAYER                                                        │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────┐      │
│  │ CF: sms-     │  │ Dialogflow   │  │ Cloud Run:           │      │
│  │ notify       │  │ CX Agent     │  │ voice-interface      │      │
│  │ (Twilio SMS) │  │ (Webhook CF) │  │ (STT/TTS/Twilio)    │      │
│  └──────────────┘  └──────────────┘  └─────────────────────┘      │
│                                                                       │
│  LEGAL AID LAYER                                                     │
│  ┌─────────────────────┐    ┌──────────────────────────────┐       │
│  │ Cloud Run            │    │ Cloud Function                │       │
│  │ dashboard-api        │    │ brief-generator               │       │
│  │ (FastAPI + Firebase  │    │ (Gemini 1.5 Pro + reportlab) │       │
│  │  Auth)              │    └──────────────────────────────┘       │
│  └─────────────────────┘                                            │
│                                                                       │
│  DATA LAYER                                                          │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │Firestore │  │ BigQuery  │  │   GCS    │  │ Secret Manager   │  │
│  │(real-time│  │(analytics │  │(files +  │  │(credentials)     │  │
│  │ docs)    │  │ + BQML)   │  │ briefs)  │  │                  │  │
│  └──────────┘  └───────────┘  └──────────┘  └──────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

1. **Idempotency everywhere**: Case numbers are document IDs in Firestore and deduplication keys in BigQuery. Re-processing the same filing twice produces exactly one record.

2. **Confidence gating**: Document AI extractions below 0.75 on critical fields route to human review rather than poisoning the AI analysis.

3. **No credentials in env vars**: Every credential is retrieved from Secret Manager at runtime. Terraform provisions the secrets; Cloud Run/Functions never have credentials baked in.

4. **Gemini as information tool, not legal advisor**: The system prompt, Pydantic schema validators, and immutable disclaimer field enforce the legal information vs. legal advice distinction at every layer.

5. **The data moat**: The landlord behavioral intelligence database (BigQuery) and the defense_effectiveness BQML model compound over time. After 12–18 months of outcome data, the system produces significantly more accurate defense confidence scores than any competitor could replicate from scratch.

6. **Multi-jurisdiction from day one**: The JurisdictionRuleset YAML architecture means adding a new state requires only a new YAML file — no code changes to the analysis engine.
