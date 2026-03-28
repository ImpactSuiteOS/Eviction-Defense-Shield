"""
EvictionShield — Module 5B: Attorney Brief Generator

Cloud Function that generates a one-page attorney brief for each case using
Gemini 1.5 Pro, formats it as PDF using reportlab, stores it in Cloud Storage,
and returns a signed URL via the GET /cases/{case_number} API endpoint.

Triggered by Firestore write to case_analyses where assigned_attorney_uid is set.

Environment variables:
    GCP_PROJECT_ID
    GCP_LOCATION                Vertex AI region
    GEMINI_MODEL                (default: gemini-1.5-pro-001)
    STORAGE_BUCKET_BRIEFS       Cloud Storage bucket for brief PDFs
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import functions_framework
from google.cloud import firestore, storage
import vertexai
from vertexai.generative_models import GenerationConfig, GenerativeModel
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
LOCATION: str = os.environ.get("GCP_LOCATION", "us-central1")
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-1.5-pro-001")
STORAGE_BUCKET_BRIEFS: str = os.environ.get("STORAGE_BUCKET_BRIEFS", "evictionshield-attorney-briefs")

vertexai.init(project=PROJECT_ID, location=LOCATION)
_model = GenerativeModel(GEMINI_MODEL)
_db = firestore.Client(project=PROJECT_ID)
_storage_client = storage.Client(project=PROJECT_ID)


# ---------------------------------------------------------------------------
# Gemini brief generation prompt
# ---------------------------------------------------------------------------

_BRIEF_SYSTEM_PROMPT = """\
You are a legal research assistant preparing a pre-hearing brief for a housing attorney.
The brief is a factual summary document — it organizes the case information for the attorney's
review. It does NOT constitute legal advice to the tenant.

Write in precise, professional legal language appropriate for an attorney audience.
Use Bluebook citation format for all statute and case law references.
Organize the brief exactly according to the structure requested.
Be specific — cite exact dates, amounts, and statutory text where available.

IMPORTANT: The brief contains only facts from the filing, analysis, and intake.
Do not add fabricated facts. If information is unavailable, say "Not available in record."
"""

_BRIEF_USER_PROMPT_TEMPLATE = """\
Prepare an attorney brief for the following eviction defense case.
Return a JSON object with exactly these fields — no prose outside the JSON:

{{
  "case_summary": "2-3 sentence summary of filing facts, hearing date, filing reason, claimed amount",
  "defenses_with_citations": [
    {{
      "defense_name": "human-readable defense name",
      "legal_basis_bluebook": "Bluebook-formatted citation",
      "factual_support": "specific facts from the record that support this defense",
      "strength_assessment": "why this defense is high/medium/low confidence",
      "lead_with_this": true or false
    }}
  ],
  "landlord_profile_summary": "2-3 sentences on filing history, withdrawal rate, complaint history",
  "tenant_intake_summary": [
    "Bullet point facts from tenant intake responses"
  ],
  "recommended_strategy": "Which defenses to lead with, in what order, and why. Include tactical notes.",
  "evidence_checklist_ranked": [
    {{"item": "document name", "priority": "critical|high|medium", "reason": "why this matters"}}
  ],
  "estimated_prep_time_hours": float,
  "time_sensitive_deadlines": [
    {{"action": "what must be done", "deadline": "YYYY-MM-DD or 'at hearing'"}}
  ]
}}

CASE DATA:
{case_data_json}

ANALYSIS DATA:
{analysis_data_json}

LANDLORD PROFILE:
{landlord_profile_json}

TENANT INTAKE:
{intake_data_json}
"""


def _generate_brief_content(
    case_data: Dict[str, Any],
    analysis_data: Dict[str, Any],
    intake_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Call Gemini to generate structured brief content."""
    landlord_profile = analysis_data.get("landlord_profile", {})

    user_prompt = _BRIEF_USER_PROMPT_TEMPLATE.format(
        case_data_json=json.dumps(case_data, indent=2, default=str),
        analysis_data_json=json.dumps({
            "case_number": analysis_data.get("case_number"),
            "defenses_identified": analysis_data.get("defenses_identified", []),
            "overall_case_strength": analysis_data.get("overall_case_strength"),
            "hearing_date": analysis_data.get("hearing_date"),
            "days_until_hearing": analysis_data.get("days_until_hearing"),
            "routing_recommendation": analysis_data.get("routing_recommendation"),
        }, indent=2, default=str),
        landlord_profile_json=json.dumps(landlord_profile, indent=2),
        intake_data_json=json.dumps(intake_data, indent=2, default=str),
    )

    response = _model.generate_content(
        [_BRIEF_SYSTEM_PROMPT, user_prompt],
        generation_config=GenerationConfig(
            temperature=0.1,
            max_output_tokens=4096,
            response_mime_type="application/json",
        ),
    )
    raw = response.candidates[0].content.parts[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(raw)


# ---------------------------------------------------------------------------
# PDF generation via reportlab
# ---------------------------------------------------------------------------

def _generate_pdf(
    brief_content: Dict[str, Any],
    case_number: str,
    hearing_date: str,
    days_until_hearing: int,
) -> bytes:
    """
    Generate a professionally formatted attorney brief PDF.
    Returns raw PDF bytes.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    # Custom styles
    title_style = ParagraphStyle(
        "BriefTitle",
        parent=styles["Heading1"],
        fontSize=14,
        textColor=colors.HexColor("#1a3a5c"),
        spaceAfter=6,
        alignment=TA_CENTER,
    )
    header_style = ParagraphStyle(
        "SectionHeader",
        parent=styles["Heading2"],
        fontSize=11,
        textColor=colors.HexColor("#1a3a5c"),
        spaceBefore=12,
        spaceAfter=4,
        borderPad=2,
    )
    body_style = ParagraphStyle(
        "BriefBody",
        parent=styles["Normal"],
        fontSize=9,
        leading=13,
        alignment=TA_JUSTIFY,
    )
    bullet_style = ParagraphStyle(
        "BriefBullet",
        parent=styles["Normal"],
        fontSize=9,
        leading=13,
        leftIndent=12,
        bulletIndent=0,
    )
    caveat_style = ParagraphStyle(
        "Caveat",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.grey,
        alignment=TA_CENTER,
        spaceBefore=6,
    )

    story = []

    # --- Header ---
    story.append(Paragraph("EVICTIONSHIELD — ATTORNEY CASE BRIEF", title_style))
    story.append(Paragraph(
        f"Case No: {case_number} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"Hearing: {hearing_date} ({days_until_hearing} days) &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        ParagraphStyle("SubHeader", parent=styles["Normal"], fontSize=9, alignment=TA_CENTER, textColor=colors.grey),
    ))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a3a5c")))
    story.append(Spacer(1, 0.1 * inch))

    # --- Disclaimer ---
    story.append(Paragraph(
        "⚠ ATTORNEY WORK PRODUCT — This brief contains legal information prepared to assist counsel. "
        "It does not constitute a complete legal analysis. Attorney must independently verify all facts and citations.",
        ParagraphStyle("Warning", parent=styles["Normal"], fontSize=8,
                       textColor=colors.HexColor("#8B0000"), alignment=TA_CENTER),
    ))
    story.append(Spacer(1, 0.15 * inch))

    # --- 1. Case Summary ---
    story.append(Paragraph("I. CASE SUMMARY", header_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Paragraph(brief_content.get("case_summary", "Not available"), body_style))
    story.append(Spacer(1, 0.1 * inch))

    # --- 2. Defenses Identified ---
    story.append(Paragraph("II. DEFENSES IDENTIFIED", header_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))

    defenses = brief_content.get("defenses_with_citations", [])
    for i, defense in enumerate(defenses, 1):
        lead_marker = " ★ LEAD DEFENSE" if defense.get("lead_with_this") else ""
        story.append(Paragraph(
            f"<b>{i}. {defense.get('defense_name', '')}{lead_marker}</b>",
            ParagraphStyle("DefenseName", parent=styles["Normal"], fontSize=10,
                           textColor=colors.HexColor("#1a3a5c")),
        ))
        story.append(Paragraph(
            f"<b>Legal Basis:</b> {defense.get('legal_basis_bluebook', 'N/A')}",
            body_style,
        ))
        story.append(Paragraph(
            f"<b>Factual Support:</b> {defense.get('factual_support', 'N/A')}",
            body_style,
        ))
        story.append(Paragraph(
            f"<b>Strength:</b> {defense.get('strength_assessment', 'N/A')}",
            body_style,
        ))
        story.append(Spacer(1, 0.08 * inch))

    # --- 3. Landlord Profile ---
    story.append(Paragraph("III. LANDLORD BEHAVIORAL PROFILE", header_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Paragraph(brief_content.get("landlord_profile_summary", "No profile data available"), body_style))
    story.append(Spacer(1, 0.1 * inch))

    # --- 4. Tenant Intake Facts ---
    story.append(Paragraph("IV. TENANT INTAKE SUMMARY", header_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    for fact in brief_content.get("tenant_intake_summary", []):
        story.append(Paragraph(f"• {fact}", bullet_style))
    story.append(Spacer(1, 0.1 * inch))

    # --- 5. Recommended Strategy ---
    story.append(Paragraph("V. RECOMMENDED LITIGATION STRATEGY", header_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Paragraph(brief_content.get("recommended_strategy", "N/A"), body_style))
    story.append(Spacer(1, 0.1 * inch))

    # --- 6. Evidence Checklist ---
    story.append(Paragraph("VI. EVIDENCE CHECKLIST (BY PRIORITY)", header_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))

    checklist = brief_content.get("evidence_checklist_ranked", [])
    if checklist:
        table_data = [["Priority", "Document", "Why It Matters"]]
        for item in checklist:
            priority = item.get("priority", "medium")
            color_map = {"critical": "🔴", "high": "🟠", "medium": "🟡"}
            table_data.append([
                f"{color_map.get(priority, '⚪')} {priority.upper()}",
                item.get("item", ""),
                item.get("reason", ""),
            ])
        t = Table(table_data, colWidths=[0.9 * inch, 2.8 * inch, 3.3 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3a5c")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4f8")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("WORDWRAP", (0, 0), (-1, -1), True),
        ]))
        story.append(t)

    story.append(Spacer(1, 0.1 * inch))

    # --- 7. Time-Sensitive Deadlines ---
    deadlines = brief_content.get("time_sensitive_deadlines", [])
    if deadlines:
        story.append(Paragraph("VII. TIME-SENSITIVE DEADLINES", header_style))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
        for dl in deadlines:
            story.append(Paragraph(
                f"• <b>{dl.get('deadline', 'TBD')}:</b> {dl.get('action', '')}",
                bullet_style,
            ))
        story.append(Spacer(1, 0.1 * inch))

    # --- Estimated prep time ---
    prep_hours = brief_content.get("estimated_prep_time_hours", 0)
    story.append(Paragraph(
        f"<b>Estimated Case Preparation Time:</b> {prep_hours:.1f} hours",
        body_style,
    ))

    # --- Footer ---
    story.append(Spacer(1, 0.2 * inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1a3a5c")))
    story.append(Paragraph(
        "This brief was generated by EvictionShield AI. All citations and facts must be independently verified. "
        "This document does not create an attorney-client relationship and is not legal advice.",
        caveat_style,
    ))

    doc.build(story)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Cloud Storage upload and signed URL
# ---------------------------------------------------------------------------

def _upload_brief_and_get_url(pdf_bytes: bytes, case_number: str) -> str:
    """Upload PDF to Cloud Storage and return a 2-hour signed URL."""
    import datetime as dt
    blob_name = f"briefs/{case_number.replace('/', '_')}/brief.pdf"
    bucket = _storage_client.bucket(STORAGE_BUCKET_BRIEFS)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(pdf_bytes, content_type="application/pdf")
    blob.metadata = {"case_number": case_number, "generated_at": datetime.utcnow().isoformat()}
    blob.patch()
    url = blob.generate_signed_url(
        version="v4",
        expiration=dt.timedelta(hours=2),
        method="GET",
    )
    logger.info("Brief uploaded: gs://%s/%s", STORAGE_BUCKET_BRIEFS, blob_name)
    return url


# ---------------------------------------------------------------------------
# Cloud Function entrypoint
# ---------------------------------------------------------------------------

@functions_framework.cloud_event
def generate_brief_on_assignment(cloud_event) -> None:
    """
    Triggered by Firestore update to case_analyses when assigned_attorney_uid is set.
    """
    data = cloud_event.data
    value = data.get("value", {})
    fields = value.get("fields", {})

    case_number = fields.get("case_number", {}).get("stringValue", "")
    attorney_uid = fields.get("assigned_attorney_uid", {}).get("stringValue", "")

    if not case_number or not attorney_uid:
        return  # Not a case assignment event

    logger.info("Generating brief for case %s (attorney %s)", case_number, attorney_uid[:8])

    doc_id = case_number.replace("/", "_")

    # Load all data
    analysis_doc = _db.collection("case_analyses").document(doc_id).get()
    filing_doc = _db.collection("eviction_filings").document(doc_id).get()
    intake_doc = _db.collection("tenant_intake_responses").document(doc_id).get()

    if not analysis_doc.exists:
        logger.error("No analysis found for case %s", case_number)
        return

    analysis_data = analysis_doc.to_dict()
    case_data = filing_doc.to_dict() if filing_doc.exists else {}
    intake_data = intake_doc.to_dict() if intake_doc.exists else {}

    hearing_date = str(analysis_data.get("hearing_date", ""))
    days_until = int(analysis_data.get("days_until_hearing", 0))

    # Generate brief content via Gemini
    try:
        brief_content = _generate_brief_content(case_data, analysis_data, intake_data)
    except Exception as exc:
        logger.error("Gemini brief generation failed: %s", exc)
        # Create minimal brief content from raw data
        brief_content = {
            "case_summary": f"Case {case_number}. Hearing: {hearing_date}.",
            "defenses_with_citations": [],
            "landlord_profile_summary": "Profile data unavailable.",
            "tenant_intake_summary": ["Intake data unavailable."],
            "recommended_strategy": "Review case materials with client.",
            "evidence_checklist_ranked": [],
            "estimated_prep_time_hours": 1.0,
            "time_sensitive_deadlines": [],
        }

    # Generate PDF
    pdf_bytes = _generate_pdf(brief_content, case_number, hearing_date, days_until)

    # Upload and get URL
    signed_url = _upload_brief_and_get_url(pdf_bytes, case_number)

    # Update Firestore with brief URL
    _db.collection("case_analyses").document(doc_id).update({
        "attorney_brief_url": signed_url,
        "brief_generated_at": datetime.utcnow().isoformat(),
    })

    logger.info("Brief generation complete for case %s", case_number)
