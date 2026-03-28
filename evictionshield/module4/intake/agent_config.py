"""
EvictionShield — Module 4B: Vertex AI Agent Builder Configuration

This module defines the complete Dialogflow CX agent configuration as Python
data structures. Use the Dialogflow CX API or gcloud CLI to apply this config.

Apply with:
    from agent_config import export_agent_config
    config = export_agent_config()
    # Then POST to: projects/{project}/locations/{location}/agents via REST API

All intents, entity types, flows, pages, and routes are fully specified.
"""

from __future__ import annotations

from typing import Any, Dict, List

AGENT_DISPLAY_NAME = "EvictionShield Housing Information Assistant"
AGENT_DEFAULT_LANGUAGE = "en"
AGENT_TIME_ZONE = "America/New_York"

# ---------------------------------------------------------------------------
# Entity Types
# ---------------------------------------------------------------------------

ENTITY_TYPES: List[Dict[str, Any]] = [
    {
        "displayName": "yes_no",
        "kind": "KIND_MAP",
        "entities": [
            {"value": "yes", "synonyms": ["yes", "yeah", "yep", "correct", "right", "i did", "i have", "i do", "sure", "absolutely"]},
            {"value": "no", "synonyms": ["no", "nope", "nah", "i didn't", "i haven't", "i don't", "not really", "no i did not"]},
            {"value": "unsure", "synonyms": ["not sure", "i don't know", "maybe", "unsure", "i think so", "possibly", "not certain"]},
        ],
        "autoExpansionMode": "AUTO_EXPANSION_MODE_DEFAULT",
    },
    {
        "displayName": "notice_delivery_method",
        "kind": "KIND_MAP",
        "entities": [
            {"value": "in_person", "synonyms": ["hand delivered", "gave it to me", "handed to me", "in person", "directly"]},
            {"value": "taped_to_door", "synonyms": ["taped to door", "left on door", "posted on door", "stuck on door", "door"]},
            {"value": "mailed", "synonyms": ["mailed", "mail", "post", "sent by mail", "first class", "regular mail"]},
            {"value": "certified_mail", "synonyms": ["certified mail", "registered mail", "return receipt", "green card", "certified"]},
            {"value": "email_or_text", "synonyms": ["email", "text", "text message", "sms", "online"]},
            {"value": "unknown", "synonyms": ["don't know", "not sure", "i don't remember", "unclear"]},
        ],
    },
    {
        "displayName": "case_number_pattern",
        "kind": "KIND_REGEXP",
        "entities": [
            {"value": "case_number", "synonyms": ["[A-Z]{2}-[A-Z]+-\\d{4}-\\d+", "\\d{2,4}-[A-Z]+-\\d+"]},
        ],
        "enableFuzzyExtraction": True,
    },
]

# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------

INTENTS: List[Dict[str, Any]] = [
    {
        "displayName": "provide_case_number",
        "trainingPhrases": [
            {"parts": [{"text": "my case number is "}, {"text": "PA-MDJ-2024-001234", "parameterId": "case_number"}]},
            {"parts": [{"text": "case "}, {"text": "LT-2024-5678", "parameterId": "case_number"}]},
            {"parts": [{"text": "it's "}, {"text": "2024-LT-00099", "parameterId": "case_number"}]},
            {"parts": [{"text": "the number on the paper is "}, {"text": "MJ-05201-LT-2024-123", "parameterId": "case_number"}]},
        ],
        "parameters": [
            {
                "id": "case_number",
                "entityType": "case_number_pattern",
                "displayName": "case_number",
                "isList": False,
            }
        ],
    },
    {
        "displayName": "confirm_eviction_received",
        "trainingPhrases": [
            {"parts": [{"text": "yes I received an eviction notice"}]},
            {"parts": [{"text": "I got papers from the court"}]},
            {"parts": [{"text": "my landlord is trying to evict me"}]},
            {"parts": [{"text": "I was served with eviction papers"}]},
            {"parts": [{"text": "I received a notice to leave"}]},
            {"parts": [{"text": "I have a court date coming up"}]},
        ],
    },
    {
        "displayName": "provide_yes_no_answer",
        "trainingPhrases": [
            {"parts": [{"text": "yes"}, {"text": " I did"}]},
            {"parts": [{"text": "no I didn't"}]},
            {"parts": [{"text": "yes"}]},
            {"parts": [{"text": "no"}]},
        ],
        "parameters": [
            {
                "id": "yes_no_answer",
                "entityType": "yes_no",
                "displayName": "answer",
                "isList": False,
            }
        ],
    },
    {
        "displayName": "request_checklist_by_email",
        "trainingPhrases": [
            {"parts": [{"text": "send it to my email"}]},
            {"parts": [{"text": "email me the list"}]},
            {"parts": [{"text": "can you send me the checklist"}]},
            {"parts": [{"text": "text me the documents"}]},
            {"parts": [{"text": "send to my phone"}]},
        ],
    },
    {
        "displayName": "escalate_to_live_agent",
        "trainingPhrases": [
            {"parts": [{"text": "I need to talk to a real person"}]},
            {"parts": [{"text": "can I speak to someone"}]},
            {"parts": [{"text": "connect me to a lawyer"}]},
            {"parts": [{"text": "I need an attorney"}]},
            {"parts": [{"text": "transfer me"}]},
        ],
    },
]

# ---------------------------------------------------------------------------
# Pages (conversation states)
# ---------------------------------------------------------------------------

PAGES: List[Dict[str, Any]] = [
    # -----------------------------------------------------------------------
    # START PAGE — Introduction
    # -----------------------------------------------------------------------
    {
        "displayName": "Start",
        "entryFulfillment": {
            "messages": [
                {
                    "text": {
                        "text": [
                            "Hello. I'm the EvictionShield Housing Information Assistant. "
                            "I'm not a lawyer and I can't give you legal advice or tell you "
                            "what will happen in your case. What I can do is help you understand "
                            "your situation and connect you with free legal help."
                            "\n\nHave you received an eviction notice or court papers?"
                        ]
                    }
                }
            ]
        },
        "transitionRoutes": [
            {
                "intent": "confirm_eviction_received",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["I understand. Let me look up your case. What is the case number on your court papers?"]}}]
                },
                "targetPage": "Collect Case Number",
            },
            {
                "condition": "$no-match-1",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["I'm here to help if you've received eviction papers or a court notice. Did you get any papers from your landlord or a court?"]}}]
                },
            },
        ],
    },

    # -----------------------------------------------------------------------
    # Phase 1 — Situation Confirmation
    # -----------------------------------------------------------------------
    {
        "displayName": "Collect Case Number",
        "form": {
            "parameters": [
                {
                    "displayName": "case_number",
                    "entityType": "case_number_pattern",
                    "required": True,
                    "fillBehavior": {
                        "initialPromptFulfillment": {
                            "messages": [{"text": {"text": ["Please tell me the case number. It should be on the top of the court papers you received."]}}]
                        },
                        "repromptEventHandlers": [
                            {
                                "event": "sys.no-match-1",
                                "triggerFulfillment": {
                                    "messages": [{"text": {"text": ["I didn't catch that. The case number is usually printed at the top of your court papers. It might look like 'MJ-05201-LT-2024-123'. Can you read it to me?"]}}]
                                },
                            }
                        ],
                    },
                }
            ]
        },
        "transitionRoutes": [
            {
                "condition": "$page.params.status = 'FINAL'",
                "targetPage": "Confirm Case Details",
                "triggerFulfillment": {
                    "webhook": "evictionshield-webhook",
                    "tag": "confirm_case",
                },
            }
        ],
    },
    {
        "displayName": "Confirm Case Details",
        "entryFulfillment": {
            "webhook": "evictionshield-webhook",
            "tag": "fetch_case_context",
        },
        "transitionRoutes": [
            {
                "condition": "$session.params.case_found = true",
                "targetPage": "Ask About Landlord Contact",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["Is that hearing date correct? And have you been in contact with your landlord recently about repairs, complaints, or rent?"]}}]
                },
            },
            {
                "condition": "$session.params.case_found = false",
                "targetPage": "Case Not Found",
            },
        ],
    },
    {
        "displayName": "Ask About Landlord Contact",
        "transitionRoutes": [
            {
                "intent": "provide_yes_no_answer",
                "condition": "$session.params.yes_no_answer = 'yes'",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["That's important information. I'll make note of that."]}}]
                },
                "targetPage": "Phase 2 Notice Questions",
            },
            {
                "intent": "provide_yes_no_answer",
                "condition": "$session.params.yes_no_answer = 'no'",
                "targetPage": "Phase 2 Notice Questions",
            },
        ],
    },

    # -----------------------------------------------------------------------
    # Phase 2 — Defense-Relevant Intake
    # -----------------------------------------------------------------------
    {
        "displayName": "Phase 2 Notice Questions",
        "entryFulfillment": {
            "messages": [{"text": {"text": ["I have a few questions to help identify what resources might be available. Did you receive a written notice from your landlord before this court filing? Something like a 'pay or quit' letter or a notice to leave?"]}}]
        },
        "form": {
            "parameters": [
                {
                    "displayName": "received_written_notice",
                    "entityType": "yes_no",
                    "required": True,
                    "fillBehavior": {
                        "initialPromptFulfillment": {"messages": []},
                    },
                }
            ]
        },
        "transitionRoutes": [
            {
                "condition": "$page.params.status = 'FINAL'",
                "targetPage": "Ask Notice Days",
            }
        ],
    },
    {
        "displayName": "Ask Notice Days",
        "entryFulfillment": {
            "messages": [{"text": {"text": ["How many days before this court filing did you receive that notice? If you're not sure of the exact number, an estimate is fine."]}}]
        },
        "form": {
            "parameters": [
                {
                    "displayName": "notice_days_given",
                    "entityType": "sys.number",
                    "required": False,
                }
            ]
        },
        "transitionRoutes": [
            {
                "condition": "true",
                "targetPage": "Ask Notice Delivery",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["How was the notice delivered to you? For example: handed to you in person, mailed, taped to your door, or by certified mail with a signature?"]}}]
                },
            }
        ],
    },
    {
        "displayName": "Ask Notice Delivery",
        "form": {
            "parameters": [
                {
                    "displayName": "notice_delivery_method",
                    "entityType": "notice_delivery_method",
                    "required": True,
                    "fillBehavior": {
                        "initialPromptFulfillment": {"messages": []},
                    },
                }
            ]
        },
        "transitionRoutes": [
            {
                "condition": "$page.params.status = 'FINAL'",
                "targetPage": "Ask Rent After Notice",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["After you received that notice, have you made any rent payments to your landlord?"]}}]
                },
            }
        ],
    },
    {
        "displayName": "Ask Rent After Notice",
        "form": {
            "parameters": [
                {
                    "displayName": "made_rent_payment_after_notice",
                    "entityType": "yes_no",
                    "required": True,
                    "fillBehavior": {"initialPromptFulfillment": {"messages": []}},
                }
            ]
        },
        "transitionRoutes": [
            {
                "condition": "$page.params.status = 'FINAL'",
                "targetPage": "Ask Habitability Complaint",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["Have you filed any complaints about the condition of your home — for example with the city housing department, by calling 311, or in writing to your landlord about repairs?"]}}]
                },
            }
        ],
    },
    {
        "displayName": "Ask Habitability Complaint",
        "form": {
            "parameters": [
                {
                    "displayName": "filed_habitability_complaint",
                    "entityType": "yes_no",
                    "required": True,
                    "fillBehavior": {"initialPromptFulfillment": {"messages": []}},
                }
            ]
        },
        "transitionRoutes": [
            {
                "condition": "$page.params.status = 'FINAL'",
                "targetPage": "Ask Lease",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["Do you have a written lease or rental agreement?"]}}]
                },
            }
        ],
    },
    {
        "displayName": "Ask Lease",
        "form": {
            "parameters": [
                {
                    "displayName": "has_written_lease",
                    "entityType": "yes_no",
                    "required": True,
                    "fillBehavior": {"initialPromptFulfillment": {"messages": []}},
                }
            ]
        },
        "transitionRoutes": [
            {
                "condition": "$page.params.status = 'FINAL'",
                "targetPage": "Ask Section 8",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["Are you a Section 8 or Housing Choice Voucher holder?"]}}]
                },
            }
        ],
    },
    {
        "displayName": "Ask Section 8",
        "form": {
            "parameters": [
                {
                    "displayName": "section_8_holder",
                    "entityType": "yes_no",
                    "required": True,
                    "fillBehavior": {"initialPromptFulfillment": {"messages": []}},
                }
            ]
        },
        "transitionRoutes": [
            {
                "condition": "$page.params.status = 'FINAL'",
                "targetPage": "Phase 3 Evidence Guidance",
            }
        ],
    },

    # -----------------------------------------------------------------------
    # Phase 3 — Evidence Guidance
    # -----------------------------------------------------------------------
    {
        "displayName": "Phase 3 Evidence Guidance",
        "entryFulfillment": {
            "webhook": "evictionshield-webhook",
            "tag": "generate_evidence_checklist",
        },
        "transitionRoutes": [
            {
                "intent": "request_checklist_by_email",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["I can text the full checklist to the phone number we have on file. Is that okay?"]}}]
                },
            },
            {
                "condition": "true",
                "targetPage": "Phase 4 Referral",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["Now let me show you the free legal aid organizations near you that may be able to help before your hearing."]}}]
                },
            },
        ],
    },

    # -----------------------------------------------------------------------
    # Phase 4 — Referral and Handoff
    # -----------------------------------------------------------------------
    {
        "displayName": "Phase 4 Referral",
        "entryFulfillment": {
            "webhook": "evictionshield-webhook",
            "tag": "get_legal_aid_orgs",
        },
        "transitionRoutes": [
            {
                "condition": "$session.params.is_urgent = true",
                "targetPage": "Urgent Transfer",
                "triggerFulfillment": {
                    "messages": [{"text": {"text": ["Because your hearing is very soon, I'm going to try to connect you with emergency assistance now."]}}]
                },
            },
            {
                "condition": "true",
                "targetPage": "Closing",
            },
        ],
    },
    {
        "displayName": "Urgent Transfer",
        "entryFulfillment": {
            "webhook": "evictionshield-webhook",
            "tag": "initiate_warm_transfer",
        },
        "transitionRoutes": [
            {
                "condition": "true",
                "targetPage": "Closing",
            }
        ],
    },
    {
        "displayName": "Closing",
        "entryFulfillment": {
            "messages": [
                {
                    "text": {
                        "text": [
                            "Remember: this conversation provided housing information, not legal advice. "
                            "Please consult a qualified attorney before making decisions about your case. "
                            "The legal aid organizations I mentioned provide free help to qualifying tenants. "
                            "\n\nGood luck with your hearing. Is there anything else I can help you with?"
                        ]
                    }
                }
            ]
        },
    },
    {
        "displayName": "Case Not Found",
        "entryFulfillment": {
            "messages": [
                {
                    "text": {
                        "text": [
                            "I wasn't able to find that case number in our system. "
                            "I can still help you find free legal aid in your area. "
                            "What is your ZIP code?"
                        ]
                    }
                }
            ]
        },
    },
]

# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

WEBHOOKS: List[Dict[str, Any]] = [
    {
        "displayName": "evictionshield-webhook",
        "genericWebService": {
            "uri": "https://us-central1-{PROJECT_ID}.cloudfunctions.net/dialogflow-webhook",
            "requestHeaders": {
                "Content-Type": "application/json",
            },
        },
        "timeout": "30s",
    }
]


# ---------------------------------------------------------------------------
# Export function
# ---------------------------------------------------------------------------

def export_agent_config(project_id: str) -> Dict[str, Any]:
    """
    Return the full agent configuration dict suitable for the Dialogflow CX REST API.
    Substitute project_id into any template placeholders.
    """
    import copy
    config = {
        "displayName": AGENT_DISPLAY_NAME,
        "defaultLanguageCode": AGENT_DEFAULT_LANGUAGE,
        "timeZone": AGENT_TIME_ZONE,
        "description": (
            "EvictionShield housing information assistant. Helps tenants understand "
            "potential procedural issues with eviction filings and connects them to "
            "free legal aid. NOT a legal advice service."
        ),
        "entityTypes": ENTITY_TYPES,
        "intents": INTENTS,
        "flows": [
            {
                "displayName": "Default Start Flow",
                "pages": PAGES,
                "nluSettings": {
                    "modelType": "MODEL_TYPE_ADVANCED",
                    "classificationThreshold": 0.3,
                },
            }
        ],
        "webhooks": [
            {**w, "genericWebService": {**w["genericWebService"], "uri": w["genericWebService"]["uri"].replace("{PROJECT_ID}", project_id)}}
            for w in WEBHOOKS
        ],
    }
    return copy.deepcopy(config)
