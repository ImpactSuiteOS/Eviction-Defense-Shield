"""
EvictionShield — Module 4B: Vertex AI Agent Builder Webhook Handler

Cloud Function webhook called by Dialogflow CX (Vertex AI Agent Builder) to:
  1. Retrieve pre-analyzed case data and inject it into the conversation context
  2. Query legal aid organizations by ZIP code
  3. Generate a personalized evidence checklist
  4. Initiate warm transfer / callback for urgent cases

The agent configuration (intents, entity types, pages, flows) is defined as a
companion YAML below this file (agent_config.py). The webhook handler is the
fulfillment backend.

Environment variables:
    GCP_PROJECT_ID
    DIALOGFLOW_AGENT_ID         Vertex AI Agent Builder agent ID
    LEGAL_AID_COLLECTION        Firestore collection (default: legal_aid_directory)
    CASE_ANALYSES_COLLECTION    Firestore collection (default: case_analyses)
    EVICTION_FILINGS_COLLECTION Firestore collection (default: eviction_filings)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import functions_framework
from google.cloud import firestore

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
LEGAL_AID_COLLECTION: str = os.environ.get("LEGAL_AID_COLLECTION", "legal_aid_directory")
CASE_ANALYSES_COLLECTION: str = os.environ.get("CASE_ANALYSES_COLLECTION", "case_analyses")
FILINGS_COLLECTION: str = os.environ.get("EVICTION_FILINGS_COLLECTION", "eviction_filings")

_db = firestore.Client(project=PROJECT_ID)


# ---------------------------------------------------------------------------
# Legal aid organization lookup
# ---------------------------------------------------------------------------

def _get_legal_aid_orgs(zip_code: str, limit: int = 3) -> List[Dict[str, Any]]:
    """
    Return the top `limit` legal aid organizations that serve the given ZIP code.
    Queries Firestore legal_aid_directory by service_zip_codes array membership.
    """
    orgs_ref = _db.collection(LEGAL_AID_COLLECTION)
    query = orgs_ref.where("service_zip_codes", "array_contains", zip_code).limit(limit)
    docs = list(query.stream())

    if not docs:
        # Fallback: query by state if ZIP not found
        state_code = _zip_to_state(zip_code)
        query = orgs_ref.where("state", "==", state_code).order_by("priority_score", direction=firestore.Query.DESCENDING).limit(limit)
        docs = list(query.stream())

    result = []
    for doc in docs:
        data = doc.to_dict()
        result.append({
            "name": data.get("organization_name", "Legal Aid Organization"),
            "phone": data.get("intake_phone", ""),
            "address": data.get("address", ""),
            "website": data.get("website", ""),
            "hours": data.get("intake_hours", "Call for hours"),
            "accepts_emergency_cases": data.get("accepts_emergency_cases", False),
            "languages_served": data.get("languages_served", ["English"]),
            "distance_miles": data.get("distance_miles"),  # Pre-computed at ingestion
        })
    return result


def _zip_to_state(zip_code: str) -> str:
    """Rough ZIP → state mapping for fallback queries."""
    prefix = int(zip_code[:3]) if zip_code[:3].isdigit() else 0
    if 150 <= prefix <= 196:
        return "PA"
    if 900 <= prefix <= 961:
        return "CA"
    if 100 <= prefix <= 119:
        return "NY"
    return "PA"


# ---------------------------------------------------------------------------
# Case data injection
# ---------------------------------------------------------------------------

def _get_case_context(case_number: str) -> Optional[Dict[str, Any]]:
    """Retrieve pre-analyzed case data from Firestore."""
    doc = _db.collection(CASE_ANALYSES_COLLECTION).document(
        case_number.replace("/", "_")
    ).get()
    if doc.exists:
        return doc.to_dict()
    return None


def _get_filing_context(case_number: str) -> Optional[Dict[str, Any]]:
    doc = _db.collection(FILINGS_COLLECTION).document(
        case_number.replace("/", "_")
    ).get()
    if doc.exists:
        return doc.to_dict()
    return None


# ---------------------------------------------------------------------------
# Evidence checklist generation
# ---------------------------------------------------------------------------

def _generate_evidence_checklist(intake_responses: Dict[str, Any], defenses: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Generate a personalized evidence checklist based on intake responses
    and the identified defenses. Returns list of {item, reason, priority} dicts.
    """
    checklist: List[Dict[str, str]] = []

    # Always include these baseline items
    checklist.append({
        "item": "A copy of your lease or rental agreement",
        "reason": "The court needs to know your tenancy terms",
        "priority": "high",
    })
    checklist.append({
        "item": "All rent receipts or bank records showing rent payments",
        "reason": "Proves your payment history",
        "priority": "high",
    })

    # Conditionally add based on intake responses
    if intake_responses.get("received_written_notice") == "yes":
        checklist.append({
            "item": "The written notice you received from your landlord",
            "reason": "The notice itself may contain procedural errors",
            "priority": "high",
        })

    if intake_responses.get("made_rent_payment_after_notice") == "yes":
        checklist.append({
            "item": "Proof of rent payment made AFTER you received the notice",
            "reason": "Payment after notice may void the eviction — this is critical evidence",
            "priority": "critical",
        })

    if intake_responses.get("filed_habitability_complaint") == "yes":
        checklist.append({
            "item": "Your complaint records (311 receipt, health department letter, emails to landlord about repairs)",
            "reason": "Documents a possible retaliatory eviction — very important",
            "priority": "critical",
        })
        checklist.append({
            "item": "Photos or videos of the housing conditions you complained about",
            "reason": "Visual evidence of habitability issues supports your defense",
            "priority": "high",
        })

    if intake_responses.get("section_8_holder") == "yes":
        checklist.append({
            "item": "Your Section 8 / Housing Choice Voucher documentation",
            "reason": "Voucher holders have additional procedural protections",
            "priority": "critical",
        })
        checklist.append({
            "item": "Any communications with your Housing Authority about this eviction",
            "reason": "The Housing Authority must be notified before evicting a voucher holder",
            "priority": "high",
        })

    if intake_responses.get("has_written_lease") == "no":
        checklist.append({
            "item": "Any written communications with your landlord (text messages, emails, letters)",
            "reason": "Establishes the terms of your tenancy without a formal lease",
            "priority": "medium",
        })

    # Defense-specific items
    defense_types = {d.get("defense_type") for d in defenses}

    if "improper_notice_period" in defense_types:
        checklist.append({
            "item": "The envelope the notice came in (shows postmark date)",
            "reason": "Proves when notice was actually delivered — critical for notice period defense",
            "priority": "critical",
        })

    if "landlord_licensure_deficiency" in defense_types:
        checklist.append({
            "item": "Check city/county records: does your landlord have a current rental license?",
            "reason": "A landlord without a valid license may not be able to evict you",
            "priority": "high",
        })

    if "improper_plaintiff" in defense_types:
        checklist.append({
            "item": "Your signed lease — check the name of the landlord on the lease vs. the court filing",
            "reason": "The party suing you may not be the correct landlord on your lease",
            "priority": "high",
        })

    return checklist


# ---------------------------------------------------------------------------
# Dialogflow CX webhook response builder
# ---------------------------------------------------------------------------

def _build_webhook_response(
    session_info_parameters: Dict[str, Any],
    fulfillment_messages: List[Dict[str, Any]],
    target_page: Optional[str] = None,
    target_flow: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a Dialogflow CX webhook response."""
    response: Dict[str, Any] = {
        "fulfillment_response": {
            "messages": fulfillment_messages,
            "merge_behavior": "REPLACE",
        },
        "session_info": {
            "parameters": session_info_parameters,
        },
    }
    if target_page:
        response["target_page"] = target_page
    if target_flow:
        response["target_flow"] = target_flow
    return response


def _text_message(text: str) -> Dict[str, Any]:
    return {"text": {"text": [text]}}


# ---------------------------------------------------------------------------
# Webhook entrypoint
# ---------------------------------------------------------------------------

@functions_framework.http
def dialogflow_webhook(request) -> tuple[str, int, Dict[str, str]]:
    """
    Dialogflow CX webhook handler.
    Routes to the correct fulfillment function based on the tag.
    """
    body = request.get_json(silent=True)
    if not body:
        return json.dumps({"error": "Empty request body"}), 400, {"Content-Type": "application/json"}

    tag: str = body.get("fulfillmentInfo", {}).get("tag", "")
    session_params: Dict[str, Any] = body.get("sessionInfo", {}).get("parameters", {})

    logger.info("Dialogflow webhook called with tag: %s", tag)

    try:
        if tag == "confirm_case":
            response = _handle_confirm_case(session_params)
        elif tag == "fetch_case_context":
            response = _handle_fetch_case_context(session_params)
        elif tag == "generate_evidence_checklist":
            response = _handle_generate_checklist(session_params)
        elif tag == "get_legal_aid_orgs":
            response = _handle_get_legal_aid(session_params)
        elif tag == "initiate_warm_transfer":
            response = _handle_warm_transfer(session_params)
        else:
            response = _build_webhook_response(
                session_params,
                [_text_message("I'm here to help. What would you like to know?")],
            )
    except Exception as exc:
        logger.error("Webhook handler error (tag=%s): %s", tag, exc)
        response = _build_webhook_response(
            session_params,
            [_text_message(
                "I'm having trouble retrieving your information right now. "
                "Please call 2-1-1 for immediate housing assistance."
            )],
        )

    return (
        json.dumps(response),
        200,
        {"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

def _handle_confirm_case(params: Dict[str, Any]) -> Dict[str, Any]:
    case_number = params.get("case_number")
    if not case_number:
        return _build_webhook_response(
            params,
            [_text_message("I wasn't able to find your case number. Could you say it again?")],
        )

    filing = _get_filing_context(case_number)
    if not filing:
        return _build_webhook_response(
            {**params, "case_found": False},
            [_text_message(
                f"I couldn't find case number {case_number} in our system. "
                "Please double-check the case number on your court notice."
            )],
        )

    hearing_date = filing.get("hearing_date", "")
    params.update({
        "case_found": True,
        "hearing_date": str(hearing_date),
        "property_address": filing.get("property_address", ""),
        "zip_code": filing.get("zip_code", ""),
        "filing_reason": filing.get("filing_reason", ""),
    })

    return _build_webhook_response(
        params,
        [_text_message(
            f"I found your case. Your hearing is scheduled for {hearing_date} at the local courthouse. "
            "I'd like to ask you a few quick questions to see what help might be available."
        )],
    )


def _handle_fetch_case_context(params: Dict[str, Any]) -> Dict[str, Any]:
    case_number = params.get("case_number")
    analysis = _get_case_context(case_number) if case_number else None

    if analysis:
        defenses = analysis.get("defenses_identified", [])
        routing = analysis.get("routing_recommendation", "standard_legal_aid")
        strength = analysis.get("overall_case_strength", "insufficient_data")
        days = analysis.get("days_until_hearing", 0)

        params.update({
            "defenses_count": len(defenses),
            "routing_recommendation": routing,
            "case_strength": strength,
            "days_until_hearing": days,
            "is_urgent": days <= 5 or routing == "urgent_legal_aid",
            "defenses_json": json.dumps(defenses),
        })

        if defenses:
            defense_names = [d.get("defense_type", "").replace("_", " ") for d in defenses[:2]]
            msg = (
                f"Our system found {len(defenses)} potential issue(s) with how this eviction was filed. "
                "This does not guarantee a particular outcome, but it may be worth discussing with "
                "a housing attorney. Let me help you prepare."
            )
        else:
            msg = (
                "Our system reviewed the filing. I'd like to ask a few questions to make sure "
                "we have the full picture before connecting you with resources."
            )
    else:
        params["defenses_count"] = 0
        params["routing_recommendation"] = "standard_legal_aid"
        params["is_urgent"] = False
        msg = (
            "I'm going to ask you a few questions about your situation "
            "to help connect you with the right resources."
        )

    return _build_webhook_response(params, [_text_message(msg)])


def _handle_generate_checklist(params: Dict[str, Any]) -> Dict[str, Any]:
    defenses_raw = params.get("defenses_json", "[]")
    try:
        defenses = json.loads(defenses_raw) if isinstance(defenses_raw, str) else defenses_raw
    except (json.JSONDecodeError, TypeError):
        defenses = []

    intake_responses = {
        "received_written_notice": params.get("received_written_notice"),
        "notice_delivery_method": params.get("notice_delivery_method"),
        "made_rent_payment_after_notice": params.get("made_rent_payment_after_notice"),
        "filed_habitability_complaint": params.get("filed_habitability_complaint"),
        "has_written_lease": params.get("has_written_lease"),
        "section_8_holder": params.get("section_8_holder"),
    }

    checklist = _generate_evidence_checklist(intake_responses, defenses)
    critical_items = [c for c in checklist if c["priority"] == "critical"]
    high_items = [c for c in checklist if c["priority"] == "high"]

    # Store full checklist as session parameter
    params["evidence_checklist_json"] = json.dumps(checklist)

    # Build a human-readable summary for the voice/chat response
    summary_parts = ["Here are the most important documents to gather before your hearing:"]
    for item in (critical_items + high_items)[:4]:
        summary_parts.append(f"• {item['item']}")

    if len(checklist) > 4:
        summary_parts.append(f"I'll send you the full list of {len(checklist)} items by text.")

    return _build_webhook_response(params, [_text_message("\n".join(summary_parts))])


def _handle_get_legal_aid(params: Dict[str, Any]) -> Dict[str, Any]:
    zip_code = params.get("zip_code", "")
    orgs = _get_legal_aid_orgs(zip_code, limit=3)

    params["legal_aid_orgs_json"] = json.dumps(orgs)
    params["legal_aid_count"] = len(orgs)

    if not orgs:
        msg = (
            "I wasn't able to find legal aid organizations specific to your area. "
            "Please call 2-1-1 (dial 2, then 1, then 1) to be connected with local housing resources."
        )
    else:
        lines = [
            f"Here are {len(orgs)} free legal aid organizations that may be able to help:"
        ]
        for i, org in enumerate(orgs, 1):
            lines.append(f"{i}. {org['name']} — {org['phone']}")
            if org.get("accepts_emergency_cases") and params.get("is_urgent"):
                lines.append(f"   They accept emergency cases.")
        msg = "\n".join(lines)

    return _build_webhook_response(params, [_text_message(msg)])


def _handle_warm_transfer(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    For urgent cases (hearing ≤ 5 days), log a callback request to Firestore
    and notify the legal aid organization's intake line.
    """
    case_number = params.get("case_number")
    zip_code = params.get("zip_code", "")
    orgs = _get_legal_aid_orgs(zip_code, limit=1)

    # Log callback request
    if case_number:
        _db.collection("callback_requests").document(case_number).set({
            "case_number": case_number,
            "requested_at": datetime.utcnow().isoformat(),
            "zip_code": zip_code,
            "days_until_hearing": params.get("days_until_hearing"),
            "assigned_org": orgs[0]["name"] if orgs else None,
            "status": "pending",
        }, merge=True)

    if orgs and orgs[0].get("accepts_emergency_cases"):
        org = orgs[0]
        msg = (
            f"Because your hearing is very soon, I've flagged your case as urgent for "
            f"{org['name']}. They will try to call you back. Their number is {org['phone']}. "
            "It is very important to also call them yourself as soon as possible."
        )
    else:
        msg = (
            "Your hearing is very soon. Please call a legal aid organization right away. "
            "Call 2-1-1 and say 'housing emergency' to be connected immediately."
        )

    return _build_webhook_response(params, [_text_message(msg)])
