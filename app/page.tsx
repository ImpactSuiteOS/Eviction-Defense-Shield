'use client';

import { useState } from 'react';
import type { ReactNode } from 'react';

type Tab = 'flow' | 'arch' | 'cost' | 'timeline';

interface StageData {
  numColor: string;
  icon: string;
  title: string;
  sub: string;
  tags: Array<{ label: string; c: string }>;
  steps: ReactNode[];
}

const stages: StageData[] = [
  {
    numColor: 'bg-blue',
    icon: '🏛',
    title: 'Court Filing Ingestion',
    sub: 'Module 1A · Cloud Run',
    tags: [
      { label: 'PA UJS Adapter', c: 'bg-blue' },
      { label: 'OAuth2', c: 'bg-orange' },
      { label: 'Secret Manager', c: 'bg-green' },
      { label: 'GCS Upload', c: 'bg-purple' },
      { label: 'Pub/Sub', c: 'bg-teal' },
    ],
    steps: [
      <>Authenticate via OAuth2 client credentials → <em>Secret Manager: court-api-creds-pa</em></>,
      <>Paginated OData fetch from <em>PA UJS REST API</em> (since last checkpoint)</>,
      <>Download filing documents (PDF / XML / JSON) + ZIP extraction</>,
      <>Upload raw binary → <em>GCS: gs://[project]-raw-filings/pa/YYYY/MM/DD/[case].pdf</em></>,
      <>Publish <em>NormalizedFiling</em> → <em>Pub/Sub: raw-eviction-filings</em></>,
      <>Checkpoint write → <em>Firestore: ingestion_checkpoints/&#123;adapter_id&#125;</em></>,
      <>Exponential backoff on all external calls; SIGTERM-safe shutdown loop</>,
    ],
  },
  {
    numColor: 'bg-blue',
    icon: '📄',
    title: 'Document Processing',
    sub: 'Module 1B · Cloud Function (push sub)',
    tags: [
      { label: 'Document AI', c: 'bg-blue' },
      { label: 'Form Parser', c: 'bg-orange' },
      { label: 'OCR', c: 'bg-orange' },
      { label: 'Confidence Gate 0.75', c: 'bg-green' },
      { label: 'Firestore', c: 'bg-purple' },
    ],
    steps: [
      <>Triggered by <em>Pub/Sub: raw-eviction-filings</em> push subscription</>,
      <>Route by format: PDF → <em>Document AI Form Parser</em> · Image → <em>OCR Processor</em> · XML/JSON → direct schema map</>,
      <>Extract 13 structured fields with per-field confidence scores</>,
      <><strong>Confidence ≥ 0.75:</strong> write to <em>Firestore: eviction_filings</em> (set/merge, idempotent) + publish <em>Pub/Sub: structured-filings-ready</em></>,
      <><strong>Confidence &lt; 0.75:</strong> partial write + route to <em>Pub/Sub: low-confidence-filings</em> (human review)</>,
    ],
  },
  {
    numColor: 'bg-purple',
    icon: '🤖',
    title: 'Gemini Legal Analysis',
    sub: 'Module 2 · Cloud Function + Vertex AI',
    tags: [
      { label: 'Gemini 1.5 Pro', c: 'bg-purple' },
      { label: '12 Defense Types', c: 'bg-blue' },
      { label: 'BQML Probabilities', c: 'bg-orange' },
      { label: 'Pydantic Validation', c: 'bg-green' },
      { label: 'Jurisdiction YAML', c: 'bg-teal' },
    ],
    steps: [
      <>Load <em>JurisdictionRuleset</em> from GCS (PA-UJS.yaml or CA-COURTS.yaml, cached in-memory)</>,
      <>Lookup landlord behavioral profile → <em>BigQuery: evictionshield.landlord_profiles</em></>,
      <>Fetch defense probability scores → <em>BQML: defense_effectiveness_model</em> (logistic regression)</>,
      <>Build prompt: BASE_SYSTEM_PROMPT + jurisdiction rules + user filing data + landlord profile</>,
      <>Call <em>Gemini 1.5 Pro</em> with <code>response_mime_type=&quot;application/json&quot;</code></>,
      <>Pydantic validate → <em>EvictionAnalysisResult</em> (12 defense types, legal basis citations, disclaimer)</>,
      <>On schema failure: automatic correction-prompt retry (max 1 attempt)</>,
      <>Write → <em>Firestore: case_analyses/&#123;case_number&#125;</em> (idempotent)</>,
      <>Publish → <em>Pub/Sub: case-analysis-complete</em></>,
    ],
  },
  {
    numColor: 'bg-orange',
    icon: '📱',
    title: 'Tenant SMS Notification',
    sub: 'Module 4A · Cloud Function + Twilio',
    tags: [
      { label: 'Twilio SMS', c: 'bg-orange' },
      { label: 'Gemini Flash', c: 'bg-purple' },
      { label: '4 Languages', c: 'bg-blue' },
      { label: 'Cloud Tasks', c: 'bg-green' },
      { label: 'Privacy: Phone Hash', c: 'bg-red' },
    ],
    steps: [
      <>Triggered by new <em>Firestore: case_analyses</em> document</>,
      <>Detect tenant language via ZIP demographic map + surname heuristics (en / es / ht / vi)</>,
      <>Generate SMS via <em>Gemini 1.5 Flash</em> ≤160 chars, 6th-grade reading level</>,
      <>Enqueue delivery → <em>Cloud Tasks: sms-notifications</em> → <em>CF: send-sms-worker</em></>,
      <>Deliver via <em>Twilio REST API</em> to tenant phone</>,
      <>Log: phone stored ONLY as SHA-256 hash — never raw number in any log</>,
    ],
  },
  {
    numColor: 'bg-orange',
    icon: '💬',
    title: 'Tenant Intake',
    sub: 'Module 4B/4C · Dialogflow CX + Voice',
    tags: [
      { label: 'Dialogflow CX', c: 'bg-blue' },
      { label: 'STT / TTS', c: 'bg-purple' },
      { label: 'Twilio Voice', c: 'bg-orange' },
      { label: '4 Conversation Phases', c: 'bg-green' },
      { label: '15 Pages', c: 'bg-teal' },
    ],
    steps: [
      <><strong>Web:</strong> Tenant clicks SMS link → Dialogflow CX agent via webhook</>,
      <><strong>Voice:</strong> Tenant calls Twilio → <em>Cloud Run: voice-interface</em> → WebSocket stream</>,
      <>Phase 1: Confirm case number → fetch <em>Firestore: case_analyses</em></>,
      <>Phase 2: Defense intake (6 questions: notice, payment, habitability, retaliation)</>,
      <>Phase 3: Generate evidence checklist (intake-conditional + defense-type-conditional items)</>,
      <>Phase 4: Legal aid referral lookup → <em>Firestore: legal_aid_directory by ZIP</em></>,
      <>Urgent cases: warm transfer to attorney / callback request</>,
      <>Transcript logged → <em>Firestore: call_transcripts</em> (service-account write only)</>,
    ],
  },
  {
    numColor: 'bg-green',
    icon: '⚖️',
    title: 'Legal Aid Triage Dashboard',
    sub: 'Module 5A · Cloud Run FastAPI + Firebase Auth',
    tags: [
      { label: 'FastAPI', c: 'bg-green' },
      { label: 'Firebase Auth JWT', c: 'bg-blue' },
      { label: 'Org Isolation', c: 'bg-orange' },
      { label: 'Firestore', c: 'bg-purple' },
      { label: 'REST API', c: 'bg-teal' },
    ],
    steps: [
      <>JWT middleware: Firebase Admin decode → load <em>Firestore: legal_aid_orgs/&#123;org_id&#125;</em> coverage ZIPs</>,
      <><em>GET /cases</em>: Firestore query with 6 filters + ZIP coverage isolation + pagination</>,
      <><em>GET /cases/&#123;id&#125;</em>: merges analysis + filing + intake + GCS signed URL for brief PDF</>,
      <><em>POST /cases/&#123;id&#125;/assign</em>: verify org membership → update Firestore → Pub/Sub notify attorney</>,
      <><em>POST /cases/&#123;id&#125;/outcome</em>: write to <em>Firestore: case_outcomes</em> → triggers Module 6A</>,
      <><em>DELETE /cases/&#123;id&#125;/tenant-data</em>: nullify PII fields (GDPR/CCPA right to erasure)</>,
    ],
  },
  {
    numColor: 'bg-green',
    icon: '📋',
    title: 'Attorney Brief Generation',
    sub: 'Module 5B · Cloud Function + reportlab',
    tags: [
      { label: 'Gemini 1.5 Pro', c: 'bg-purple' },
      { label: 'reportlab PDF', c: 'bg-blue' },
      { label: 'Bluebook Citations', c: 'bg-orange' },
      { label: 'GCS Signed URL', c: 'bg-green' },
    ],
    steps: [
      <>Triggered by <em>Firestore: case_analyses</em> update (assigned_attorney_uid set)</>,
      <>Fetch: case analysis + filing + tenant intake responses</>,
      <>Call <em>Gemini 1.5 Pro</em> → structured brief content JSON (7 sections)</>,
      <>Generate PDF via <em>reportlab</em>: case summary, defenses, landlord profile, intake facts, strategy, evidence checklist, deadlines</>,
      <>All statute citations in Bluebook format; mandatory legal disclaimer embedded</>,
      <>Upload → <em>GCS: attorney-briefs/</em> · Return 2-hour signed URL → <em>Firestore: case_analyses.attorney_brief_url</em></>,
    ],
  },
  {
    numColor: 'bg-teal',
    icon: '🔁',
    title: 'Outcome Feedback Loop',
    sub: 'Module 6A · Cloud Function',
    tags: [
      { label: 'BigQuery MERGE', c: 'bg-teal' },
      { label: 'Defense Effectiveness', c: 'bg-blue' },
      { label: 'Analysis Review', c: 'bg-orange' },
      { label: 'Pub/Sub Trigger', c: 'bg-purple' },
    ],
    steps: [
      <>Triggered by new <em>Firestore: case_outcomes</em> document</>,
      <>BigQuery MERGE → <em>evictionshield.filing_events</em> (outcome, represented, outcome_date)</>,
      <>BigQuery MERGE → <em>evictionshield.defense_effectiveness</em> (increment total_cases, cases_defense_succeeded per defense type)</>,
      <>Create <em>Firestore: analysis_reviews</em> if high-confidence defense identified but tenant lost</>,
      <>Publish → <em>Pub/Sub: trigger-profile-update</em> to recalculate landlord profiles</>,
    ],
  },
  {
    numColor: 'bg-teal',
    icon: '🏠',
    title: 'Landlord Profile Updates',
    sub: 'Module 3B · Dataflow (Apache Beam)',
    tags: [
      { label: 'Dataflow', c: 'bg-blue' },
      { label: 'Apache Beam', c: 'bg-orange' },
      { label: 'Rolling 90d/365d', c: 'bg-green' },
      { label: 'Cloud Scheduler', c: 'bg-purple' },
    ],
    steps: [
      <>Cloud Scheduler triggers every 6 hours → Dataflow REST API</>,
      <><em>ReadFromBigQuery</em>: evictionshield.filing_events</>,
      <>NormalizeFilingEventFn → GroupByKey → all events per landlord</>,
      <>ComputeLandlordMetricsFn: rolling 90d/365d counts, withdrawal rate, defect rate, tenant win rate</>,
      <>ComputeComplaintCorrelationFn: side input from habitability_complaints table</>,
      <>WriteToBigQuery → <em>evictionshield.landlord_profiles</em> (WRITE_TRUNCATE)</>,
    ],
  },
  {
    numColor: 'bg-red',
    icon: '🧠',
    title: 'BQML Retraining',
    sub: 'Module 6B · Cloud Build (weekly)',
    tags: [
      { label: 'BigQuery ML', c: 'bg-red' },
      { label: 'Logistic Regression', c: 'bg-blue' },
      { label: 'Quality Gate', c: 'bg-orange' },
      { label: 'Cloud Scheduler', c: 'bg-purple' },
    ],
    steps: [
      <>Cloud Scheduler triggers every Sunday 2AM UTC → Cloud Build pipeline</>,
      <>Step 1: Validate training data volume (&gt;500 rows required)</>,
      <>Step 2: CREATE OR REPLACE MODEL — LOGISTIC_REG, 8 features, auto_class_weights=TRUE</>,
      <>Step 3: Quality gate — <em>ML.EVALUATE</em>: precision &gt;0.55, roc_auc &gt;0.65</>,
      <>Step 4: Archive snapshot to GCS · Step 5: Log retraining event to BigQuery</>,
    ],
  },
  {
    numColor: 'bg-red',
    icon: '🔒',
    title: 'Data Retention & Privacy',
    sub: 'Module 7C · Cloud Function (daily)',
    tags: [
      { label: 'PII Anonymization', c: 'bg-red' },
      { label: 'SHA-256 Tokens', c: 'bg-blue' },
      { label: '90-Day Window', c: 'bg-green' },
      { label: 'Compliance Report', c: 'bg-orange' },
    ],
    steps: [
      <>Cloud Scheduler daily 2AM UTC → Cloud Function: evictionshield-retention-job</>,
      <>Query <em>Firestore: eviction_filings</em> WHERE pii_anonymized=false AND age&gt;90d</>,
      <>Batch replace tenant_name/phone/email with deterministic <em>ANON_ SHA-256 tokens</em></>,
      <>BigQuery verification: confirm no PII columns in filing_events schema</>,
      <>Monthly (1st of month): compliance report JSON → <em>GCS: [project]-compliance/</em></>,
    ],
  },
];

export default function WorkflowPage() {
  const [tab, setTab] = useState<Tab>('flow');
  const [openStage, setOpenStage] = useState<number | null>(null);

  const toggle = (i: number) => setOpenStage(prev => prev === i ? null : i);

  return (
    <>
      <header>
        <h1>⚖️ EvictionShield</h1>
        <p>Automated Eviction Defense Identification · Google Cloud + Vertex AI</p>
        <div className="badge-row">
          <span className="badge">8 Modules</span>
          <span className="badge">11 Pipeline Stages</span>
          <span className="badge">Gemini 1.5 Pro</span>
          <span className="badge">Cloud Run</span>
          <span className="badge">BigQuery ML</span>
          <span className="badge">Dialogflow CX</span>
          <span className="badge">Twilio</span>
        </div>
      </header>

      <div className="tabs">
        {(['flow', 'arch', 'cost', 'timeline'] as Tab[]).map((t, i) => (
          <div
            key={t}
            className={`tab${tab === t ? ' active' : ''}`}
            onClick={() => setTab(t)}
          >
            {['🔄 Data Flow', '🏗 Architecture', '💰 Cost Model', '📅 90-Day Launch'][i]}
          </div>
        ))}
      </div>

      <main>
        {/* ══════════════ FLOW PANEL ══════════════ */}
        <div id="panel-flow" className={`panel${tab === 'flow' ? ' active' : ''}`}>
          <p style={{ color: 'var(--muted)', fontSize: '13px', marginBottom: '20px' }}>
            Click any stage to expand details &nbsp;·&nbsp; Court filing → Attorney brief in under 60 minutes
          </p>
          <div className="flow-wrapper">
            <div className="flow">
              {stages.map((s, i) => (
                <div key={i} className="stage" onClick={() => toggle(i)}>
                  <div className="stage-num">
                    <div className={`num-circle ${s.numColor}`}>{i + 1}</div>
                    {i < stages.length - 1 && <div className="connector" />}
                  </div>
                  <div className={`stage-card${openStage === i ? ' expanded' : ''}`}>
                    <div className="stage-header">
                      <span className="stage-icon">{s.icon}</span>
                      <div>
                        <div className="stage-title">{s.title}</div>
                        <div className="stage-sub">{s.sub}</div>
                      </div>
                    </div>
                    <div className="stage-tags">
                      {s.tags.map((t, j) => (
                        <span key={j} className={`tag ${t.c}`}>{t.label}</span>
                      ))}
                    </div>
                    <div className={`stage-detail${openStage === i ? ' open' : ''}`}>
                      <ul className="step-list">
                        {s.steps.map((step, j) => (
                          <li key={j}><span>{step}</span></li>
                        ))}
                      </ul>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* ══════════════ ARCH PANEL ══════════════ */}
        <div id="panel-arch" className={`panel${tab === 'arch' ? ' active' : ''}`}>
          <h2 className="section-heading">Service Map</h2>
          <div className="arch-grid">
            <div className="layer-card">
              <div className="layer-title c-blue">🔄 Ingestion Layer</div>
              <div className="service"><span className="svc-icon">☁️</span><div><div className="svc-name">Cloud Run: evictionshield-ingestion</div><div className="svc-desc">PA UJS OAuth2 adapter, GCS upload, Pub/Sub emit</div></div></div>
              <div className="service"><span className="svc-icon">⚡</span><div><div className="svc-name">CF: docprocessing</div><div className="svc-desc">Document AI Form Parser + OCR, confidence gating</div></div></div>
              <div className="service"><span className="svc-icon">🗄️</span><div><div className="svc-name">GCS: raw-filings</div><div className="svc-desc">Court PDFs, XMLs, ZIPs by state/date</div></div></div>
            </div>

            <div className="layer-card">
              <div className="layer-title c-purple">🤖 Intelligence Layer</div>
              <div className="service"><span className="svc-icon">⚡</span><div><div className="svc-name">CF: gemini-analysis</div><div className="svc-desc">Gemini 1.5 Pro, 12 defenses, Pydantic validation</div></div></div>
              <div className="service"><span className="svc-icon">📊</span><div><div className="svc-name">BigQuery ML</div><div className="svc-desc">defense_effectiveness_model (LOGISTIC_REG, 8 features)</div></div></div>
              <div className="service"><span className="svc-icon">📁</span><div><div className="svc-name">GCS: rulesets</div><div className="svc-desc">PA-UJS.yaml, CA-COURTS.yaml — YAML-driven jurisdiction rules</div></div></div>
            </div>

            <div className="layer-card">
              <div className="layer-title c-orange">📱 Tenant Layer</div>
              <div className="service"><span className="svc-icon">⚡</span><div><div className="svc-name">CF: sms-notify + send-sms-worker</div><div className="svc-desc">Gemini Flash SMS gen, Cloud Tasks queue, Twilio delivery</div></div></div>
              <div className="service"><span className="svc-icon">💬</span><div><div className="svc-name">Dialogflow CX + Webhook CF</div><div className="svc-desc">15 pages, 5 intents, 4 conversation phases</div></div></div>
              <div className="service"><span className="svc-icon">☁️</span><div><div className="svc-name">Cloud Run: voice-interface</div><div className="svc-desc">Twilio WebSocket, STT streaming, Neural2 TTS (4 langs)</div></div></div>
            </div>

            <div className="layer-card">
              <div className="layer-title c-green">⚖️ Legal Aid Layer</div>
              <div className="service"><span className="svc-icon">☁️</span><div><div className="svc-name">Cloud Run: dashboard-api</div><div className="svc-desc">FastAPI, Firebase Auth JWT, org isolation</div></div></div>
              <div className="service"><span className="svc-icon">⚡</span><div><div className="svc-name">CF: brief-generator</div><div className="svc-desc">Gemini 1.5 Pro → reportlab PDF, Bluebook citations</div></div></div>
              <div className="service"><span className="svc-icon">📁</span><div><div className="svc-name">GCS: attorney-briefs</div><div className="svc-desc">Generated PDFs with 2-hour signed URLs</div></div></div>
            </div>

            <div className="layer-card">
              <div className="layer-title c-teal">🔁 Feedback Layer</div>
              <div className="service"><span className="svc-icon">⚡</span><div><div className="svc-name">CF: outcome-ingest</div><div className="svc-desc">BigQuery MERGE, defense effectiveness counters</div></div></div>
              <div className="service"><span className="svc-icon">🌊</span><div><div className="svc-name">Dataflow: landlord-profile-update</div><div className="svc-desc">Apache Beam, rolling 90d/365d metrics, every 6h</div></div></div>
              <div className="service"><span className="svc-icon">🔧</span><div><div className="svc-name">Cloud Build: bqml-weekly-retrain</div><div className="svc-desc">5-step pipeline with quality gate, Sundays 2AM</div></div></div>
            </div>

            <div className="layer-card">
              <div className="layer-title c-red">🔒 Security &amp; Privacy</div>
              <div className="service"><span className="svc-icon">⚡</span><div><div className="svc-name">CF: retention-job</div><div className="svc-desc">90-day PII anonymization, SHA-256 ANON_ tokens</div></div></div>
              <div className="service"><span className="svc-icon">🔑</span><div><div className="svc-name">Secret Manager</div><div className="svc-desc">All credentials at runtime — no env var secrets</div></div></div>
              <div className="service"><span className="svc-icon">🛡️</span><div><div className="svc-name">Firestore Security Rules</div><div className="svc-desc">12 collection rules, org ZIP isolation, phone-hash ownership</div></div></div>
            </div>

            <div className="layer-card">
              <div className="layer-title c-yellow">📊 Data Layer</div>
              <div className="service"><span className="svc-icon">🔥</span><div><div className="svc-name">Firestore (Native)</div><div className="svc-desc">Real-time documents: filings, analyses, outcomes, transcripts</div></div></div>
              <div className="service"><span className="svc-icon">📊</span><div><div className="svc-name">BigQuery</div><div className="svc-desc">filing_events, landlord_profiles, defense_effectiveness, BQML</div></div></div>
              <div className="service"><span className="svc-icon">📨</span><div><div className="svc-name">Pub/Sub (7 topics + DLQs)</div><div className="svc-desc">raw-filings, structured-ready, analysis-complete, outcomes…</div></div></div>
            </div>

            <div className="layer-card">
              <div className="layer-title c-blue">🏗 Infrastructure</div>
              <div className="service"><span className="svc-icon">🔧</span><div><div className="svc-name">Terraform (Module 7)</div><div className="svc-desc">21 APIs, 8 service accounts, all GCP resources as IaC</div></div></div>
              <div className="service"><span className="svc-icon">🧪</span><div><div className="svc-name">pytest (Module 8)</div><div className="svc-desc">85% coverage requirement, 4 test suites, 20 synthetic fixtures</div></div></div>
              <div className="service"><span className="svc-icon">⏰</span><div><div className="svc-name">Cloud Scheduler</div><div className="svc-desc">Dataflow every 6h, BQML Sunday 2AM, Retention daily 2AM</div></div></div>
            </div>
          </div>
        </div>

        {/* ══════════════ COST PANEL ══════════════ */}
        <div id="panel-cost" className={`panel${tab === 'cost' ? ' active' : ''}`}>
          <h2 className="section-heading">Cost Model</h2>
          <div className="cost-grid">
            <div className="cost-card">
              <div className="cost-header">
                <div>
                  <div className="cost-title">Pilot Scale</div>
                  <div style={{ fontSize: '12px', color: 'var(--muted)' }}>1,000 filings/mo · Single city</div>
                </div>
                <div className="cost-amount c-green">$126<span style={{ fontSize: '14px', color: 'var(--muted)' }}>/mo</span></div>
              </div>
              <div className="cost-row"><span className="label">Cloud Run + Functions</span><span className="value">$60</span></div>
              <div className="cost-row"><span className="label">Gemini 1.5 Pro (analysis)</span><span className="value">$22</span></div>
              <div className="cost-row"><span className="label">Document AI Form Parser</span><span className="value">$1.50</span></div>
              <div className="cost-row"><span className="label">STT / TTS (200 calls)</span><span className="value">$12.80</span></div>
              <div className="cost-row"><span className="label">Firestore</span><span className="value">$12</span></div>
              <div className="cost-row"><span className="label">BigQuery + GCS</span><span className="value">$5.50</span></div>
              <div className="cost-row"><span className="label">Twilio SMS</span><span className="value">$7.90</span></div>
              <div className="cost-row"><span className="label">Other (Logging, Scheduler)</span><span className="value">$5</span></div>
            </div>

            <div className="cost-card">
              <div className="cost-header">
                <div>
                  <div className="cost-title">City Scale</div>
                  <div style={{ fontSize: '12px', color: 'var(--muted)' }}>50,000 filings/mo · 10 cities</div>
                </div>
                <div className="cost-amount c-orange">$5,060<span style={{ fontSize: '14px', color: 'var(--muted)' }}>/mo</span></div>
              </div>
              <div className="cost-row"><span className="label">Cloud Run + Functions</span><span className="value">$920</span></div>
              <div className="cost-row"><span className="label">Dataflow (6h runs)</span><span className="value">$450</span></div>
              <div className="cost-row"><span className="label">Gemini 1.5 Pro</span><span className="value">$1,730</span></div>
              <div className="cost-row"><span className="label">Document AI</span><span className="value">$75</span></div>
              <div className="cost-row"><span className="label">STT / TTS (10K calls)</span><span className="value">$640</span></div>
              <div className="cost-row"><span className="label">Firestore</span><span className="value">$600</span></div>
              <div className="cost-row"><span className="label">BigQuery + GCS</span><span className="value">$95</span></div>
              <div className="cost-row"><span className="label">Twilio SMS</span><span className="value">$395</span></div>
              <div className="cost-row"><span className="label">BQML + Other</span><span className="value">$155</span></div>
            </div>

            <div className="cost-card">
              <div className="cost-header">
                <div>
                  <div className="cost-title">National Scale</div>
                  <div style={{ fontSize: '12px', color: 'var(--muted)' }}>500,000 filings/mo · ~15% US evictions</div>
                </div>
                <div className="cost-amount c-red">$49,500<span style={{ fontSize: '14px', color: 'var(--muted)' }}>/mo</span></div>
              </div>
              <div className="cost-row"><span className="label">Cloud Run + Functions</span><span className="value">$7,300</span></div>
              <div className="cost-row"><span className="label">Dataflow (continuous)</span><span className="value">$4,200</span></div>
              <div className="cost-row"><span className="label">Gemini 1.5 Pro</span><span className="value">$17,300</span></div>
              <div className="cost-row"><span className="label">Document AI</span><span className="value">$750</span></div>
              <div className="cost-row"><span className="label">STT / TTS (100K calls)</span><span className="value">$6,400</span></div>
              <div className="cost-row"><span className="label">Firestore</span><span className="value">$5,800</span></div>
              <div className="cost-row"><span className="label">BigQuery + GCS</span><span className="value">$940</span></div>
              <div className="cost-row"><span className="label">Twilio SMS (volume disc.)</span><span className="value">$3,750</span></div>
              <div className="cost-row"><span className="label">Multi-region + Monitoring</span><span className="value">$3,060</span></div>
            </div>
          </div>

          <div style={{ marginTop: '24px', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: '10px', padding: '20px' }}>
            <div className="section-heading" style={{ fontSize: '15px' }}>Revenue Model</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: '16px', fontSize: '13px' }}>
              <div>
                <div style={{ color: 'var(--green)', fontWeight: 600, marginBottom: '6px' }}>Pilot ($126/mo cost)</div>
                <div style={{ color: 'var(--muted)' }}>Revenue: $500–$2,000/mo</div>
                <div style={{ color: 'var(--muted)' }}>1 legal aid org subscriber</div>
                <div style={{ color: 'var(--muted)' }}>Gross margin: ~90%+</div>
              </div>
              <div>
                <div style={{ color: 'var(--orange)', fontWeight: 600, marginBottom: '6px' }}>City Scale ($5,060/mo cost)</div>
                <div style={{ color: 'var(--muted)' }}>Revenue: $25K–$50K/mo</div>
                <div style={{ color: 'var(--muted)' }}>10 orgs @ $2,500–$5,000/mo</div>
                <div style={{ color: 'var(--muted)' }}>Gross margin: ~80–90%</div>
              </div>
              <div>
                <div style={{ color: 'var(--red)', fontWeight: 600, marginBottom: '6px' }}>National ($49.5K/mo cost)</div>
                <div style={{ color: 'var(--muted)' }}>Revenue: $500K–$1M/mo</div>
                <div style={{ color: 'var(--muted)' }}>Committed use discounts apply</div>
                <div style={{ color: 'var(--muted)' }}>Gross margin: ~90–95%</div>
              </div>
            </div>
          </div>
        </div>

        {/* ══════════════ TIMELINE PANEL ══════════════ */}
        <div id="panel-timeline" className={`panel${tab === 'timeline' ? ' active' : ''}`}>
          <h2 className="section-heading">90-Day Launch Sequence</h2>
          <div className="timeline">

            <div className="phase">
              <div className="phase-line">
                <div className="phase-dot" style={{ background: 'var(--blue)' }} />
                <div className="phase-connector" />
              </div>
              <div className="phase-body">
                <div className="phase-header">
                  <span className="phase-days">Days 1–21</span>
                  <span className="phase-name">Phase 1 — Foundation</span>
                </div>
                <div className="phase-goal">Goal: One real filing from PA UJS in Firestore with correct fields</div>
                <div style={{ fontSize: '13px', color: 'var(--muted)', marginBottom: '8px' }}>GCP setup → Terraform apply → PA UJS adapter → Document AI training → Ruleset upload → End-to-end smoke test</div>
                <div className="criteria">
                  <div className="criterion"><span className="crit-pass">✅</span><span>≥1 real PA filing ingested with ≥0.75 confidence on all critical fields</span></div>
                  <div className="criterion"><span className="crit-pass">✅</span><span>Module 8 ingestion tests passing at 85%+ coverage</span></div>
                  <div className="criterion"><span className="crit-fail">❌ STOP</span><span>if PA UJS API access denied or Document AI confidence consistently below 0.65</span></div>
                </div>
              </div>
            </div>

            <div className="phase">
              <div className="phase-line">
                <div className="phase-dot" style={{ background: 'var(--purple)' }} />
                <div className="phase-connector" />
              </div>
              <div className="phase-body">
                <div className="phase-header">
                  <span className="phase-days">Days 22–42</span>
                  <span className="phase-name">Phase 2 — Intelligence Layer</span>
                </div>
                <div className="phase-goal">Goal: Valid EvictionAnalysisResult identifying ≥1 defense correctly</div>
                <div style={{ fontSize: '13px', color: 'var(--muted)', marginBottom: '8px' }}>Gemini CF deploy → Seed BigQuery → Pydantic validation → CA ruleset → Legal review → Benchmark 20 fixtures</div>
                <div className="criteria">
                  <div className="criterion"><span className="crit-pass">✅</span><span>Gemini correctly identifies expected defense in ≥16/20 fixtures (80%)</span></div>
                  <div className="criterion"><span className="crit-pass">✅</span><span>Zero hallucinated statute citations (verified by legal reviewer)</span></div>
                  <div className="criterion"><span className="crit-pass">✅</span><span>All 20 fixture analyses complete in &lt;30 seconds each</span></div>
                  <div className="criterion"><span className="crit-fail">❌ STOP</span><span>if Gemini consistently misidentifies the improper_notice_period fixture</span></div>
                </div>
              </div>
            </div>

            <div className="phase">
              <div className="phase-line">
                <div className="phase-dot" style={{ background: 'var(--orange)' }} />
                <div className="phase-connector" />
              </div>
              <div className="phase-body">
                <div className="phase-header">
                  <span className="phase-days">Days 43–56</span>
                  <span className="phase-name">Phase 3 — Tenant Interface</span>
                </div>
                <div className="phase-goal">Goal: Tenant receives SMS within 5 minutes of filing ingestion</div>
                <div style={{ fontSize: '13px', color: 'var(--muted)', marginBottom: '8px' }}>SMS deploy (4 langs) → Dialogflow CX agent → Legal aid directory → Voice interface → Full end-to-end test</div>
                <div className="criteria">
                  <div className="criterion"><span className="crit-pass">✅</span><span>SMS received within 5 minutes of case analysis completing</span></div>
                  <div className="criterion"><span className="crit-pass">✅</span><span>Dialogflow agent completes all 4 phases without looping</span></div>
                  <div className="criterion"><span className="crit-pass">✅</span><span>Legal aid lookup returns results for Philadelphia ZIP codes</span></div>
                  <div className="criterion"><span className="crit-fail">❌ STOP</span><span>if Twilio delivery rate below 85% in sandbox testing</span></div>
                </div>
              </div>
            </div>

            <div className="phase">
              <div className="phase-line">
                <div className="phase-dot" style={{ background: 'var(--green)' }} />
                <div className="phase-connector" />
              </div>
              <div className="phase-body">
                <div className="phase-header">
                  <span className="phase-days">Days 57–70</span>
                  <span className="phase-name">Phase 4 — Legal Aid Dashboard</span>
                </div>
                <div className="phase-goal">Goal: First paying customer possible — attorney can log in and work cases</div>
                <div style={{ fontSize: '13px', color: 'var(--muted)', marginBottom: '8px' }}>Dashboard API → Brief generator (legal review) → Firestore security rules → Org isolation verification → Pilot org onboard</div>
                <div className="criteria">
                  <div className="criterion"><span className="crit-pass">✅</span><span>Attorney can log in, see only their org&apos;s cases, assign a case, download brief</span></div>
                  <div className="criterion"><span className="crit-pass">✅</span><span>Brief PDF legally accurate (verified by pilot attorney)</span></div>
                  <div className="criterion"><span className="crit-pass">✅</span><span>Org isolation verified: org A cannot read org B&apos;s cases</span></div>
                  <div className="criterion"><span className="crit-fail">❌ STOP</span><span>if pilot attorney says brief is misleading or cites incorrect statutes</span></div>
                </div>
              </div>
            </div>

            <div className="phase">
              <div className="phase-line">
                <div className="phase-dot" style={{ background: 'var(--teal)' }} />
                <div className="phase-connector" />
              </div>
              <div className="phase-body">
                <div className="phase-header">
                  <span className="phase-days">Days 71–84</span>
                  <span className="phase-name">Phase 5 — Hardening</span>
                </div>
                <div className="phase-goal">Goal: Production-ready — security audit passed, 99% uptime</div>
                <div style={{ fontSize: '13px', color: 'var(--muted)', marginBottom: '8px' }}>Outcome ingestion → BQML first run → Retention job → Security rules + pentest → Load test 100 concurrent → Monitoring alerts</div>
                <div className="criteria">
                  <div className="criterion"><span className="crit-pass">✅</span><span>Outcome data flows: API → BigQuery → defense_effectiveness table</span></div>
                  <div className="criterion"><span className="crit-pass">✅</span><span>Load test: dashboard API handles 100 concurrent @ &lt;2s P95</span></div>
                  <div className="criterion"><span className="crit-pass">✅</span><span>No critical security findings from penetration test</span></div>
                  <div className="criterion"><span className="crit-fail">❌ STOP</span><span>if PII anonymization fails or leaves raw PII after 90-day window</span></div>
                </div>
              </div>
            </div>

            <div className="phase">
              <div className="phase-line">
                <div className="phase-dot" style={{ background: 'var(--yellow)' }} />
              </div>
              <div className="phase-body">
                <div className="phase-header">
                  <span className="phase-days">Days 85–90</span>
                  <span className="phase-name">Phase 6 — Commercial Launch</span>
                </div>
                <div className="phase-goal">Goal: First revenue, first non-paying tenant reached, second city online</div>
                <div style={{ fontSize: '13px', color: 'var(--muted)', marginBottom: '8px' }}>Invoice pilot org · Press release to housing justice orgs · Onboard 2nd org + CA ruleset · Launch public intake portal</div>
                <div className="criteria">
                  <div className="criterion"><span className="crit-pass">📅 Day 21</span><span>First real filing analyzed (non-paying impact)</span></div>
                  <div className="criterion"><span className="crit-pass">📅 Day 56</span><span>First tenant reached via real SMS</span></div>
                  <div className="criterion"><span className="crit-pass">📅 Day 85</span><span>First revenue invoice sent — $2,500–$5,000</span></div>
                </div>
              </div>
            </div>

          </div>
        </div>
      </main>
    </>
  );
}
