"""
EvictionShield — Module 4C: Multilingual Voice Interface

Cloud Run service providing a phone-based tenant interface via Twilio,
Google Cloud Speech-to-Text (streaming), Google Cloud Text-to-Speech (Neural2),
and Dialogflow CX for conversation management.

Endpoints:
    POST /voice/inbound       — Twilio webhook for inbound calls (returns TwiML)
    WebSocket /voice/stream   — Twilio Media Streams audio bidirectional channel

Architecture:
    Twilio call → /voice/inbound (TwiML redirect to /voice/stream)
    /voice/stream WebSocket:
        Twilio audio → STT → Dialogflow CX → TTS → Twilio audio

Environment variables:
    GCP_PROJECT_ID
    GCP_LOCATION                    Cloud Speech/TTS region (default: us-central1)
    DIALOGFLOW_AGENT_ID             Dialogflow CX agent ID
    DIALOGFLOW_LOCATION             Dialogflow CX location (default: us-central1)
    TWILIO_SECRET_NAME              Secret Manager: Twilio account credentials
    TRANSFER_PHONE_NUMBER           Phone number for live agent warm transfer
    SILENCE_TIMEOUT_SECONDS         (default: 5)
    LOG_CALL_TRANSCRIPTS            Whether to log full transcripts (default: true)
    PORT                            Cloud Run port (default: 8080)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncGenerator, Dict, Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from google.cloud import dialogflowcx_v3 as dialogflow, firestore, secretmanager, texttospeech
from google.cloud.speech_v2 import SpeechAsyncClient
from google.cloud.speech_v2.types import cloud_speech
import audioop

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ID: str = os.environ["GCP_PROJECT_ID"]
GCP_LOCATION: str = os.environ.get("GCP_LOCATION", "us-central1")
AGENT_ID: str = os.environ["DIALOGFLOW_AGENT_ID"]
DIALOGFLOW_LOCATION: str = os.environ.get("DIALOGFLOW_LOCATION", "us-central1")
TWILIO_SECRET_NAME: str = os.environ.get("TWILIO_SECRET_NAME", "twilio-creds")
TRANSFER_PHONE: str = os.environ.get("TRANSFER_PHONE_NUMBER", "+12155551234")
SILENCE_TIMEOUT: int = int(os.environ.get("SILENCE_TIMEOUT_SECONDS", "5"))
LOG_TRANSCRIPTS: bool = os.environ.get("LOG_CALL_TRANSCRIPTS", "true").lower() == "true"
PORT: int = int(os.environ.get("PORT", "8080"))

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

_tts_client = texttospeech.TextToSpeechClient()
_firestore_client = firestore.Client(project=PROJECT_ID)
_secret_client = secretmanager.SecretManagerServiceClient()
_df_sessions_client = dialogflow.SessionsClient(
    client_options={"api_endpoint": f"{DIALOGFLOW_LOCATION}-dialogflow.googleapis.com"}
)

app = FastAPI(title="EvictionShield Voice Interface")

# ---------------------------------------------------------------------------
# Language detection and voice mapping
# ---------------------------------------------------------------------------

_LANGUAGE_VOICES: Dict[str, Dict[str, str]] = {
    "en": {"language_code": "en-US", "name": "en-US-Neural2-F", "display": "English"},
    "es": {"language_code": "es-US", "name": "es-US-Neural2-A", "display": "Spanish"},
    "ht": {"language_code": "fr-FR", "name": "fr-FR-Neural2-A", "display": "Haitian Creole"},
    "vi": {"language_code": "vi-VN", "name": "vi-VN-Neural2-A", "display": "Vietnamese"},
}

_OPENING_PROMPTS: Dict[str, str] = {
    "en": (
        "Hello. You've reached EvictionShield, a free housing information service. "
        "I can help you understand your eviction notice and find free legal help. "
        "I am not a lawyer and cannot give you legal advice. "
        "To continue in English, say 'English'. Para español, diga 'Español'."
    ),
    "es": (
        "Hola. Ha llamado a EvictionShield, un servicio gratuito de información sobre vivienda. "
        "No soy abogado y no puedo darle consejos legales. Para continuar en español, diga 'español'."
    ),
    "ht": (
        "Bonjou. Ou rele EvictionShield, yon sèvis enfòmasyon lojman gratis. "
        "Mwen pa yon avoka e mwen pa ka ba ou konsèy legal. Pou kontinye an Kreyòl, di 'Kreyòl'."
    ),
    "vi": (
        "Xin chào. Bạn đã gọi đến EvictionShield, dịch vụ thông tin nhà ở miễn phí. "
        "Tôi không phải là luật sư. Để tiếp tục bằng tiếng Việt, nói 'tiếng Việt'."
    ),
}


# ---------------------------------------------------------------------------
# Text-to-Speech synthesis
# ---------------------------------------------------------------------------

async def _synthesize_speech(text: str, language: str = "en") -> bytes:
    """
    Synthesize text to mu-law 8kHz audio (Twilio Media Streams format).
    Returns raw audio bytes.
    """
    voice_config = _LANGUAGE_VOICES.get(language, _LANGUAGE_VOICES["en"])

    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code=voice_config["language_code"],
        name=voice_config["name"],
    )
    # Twilio Media Streams require: 8kHz, 1-channel, 8-bit mu-law PCM
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MULAW,
        sample_rate_hertz=8000,
        speaking_rate=0.9,  # Slightly slower for clarity
    )
    response = _tts_client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    return response.audio_content


# ---------------------------------------------------------------------------
# Dialogflow CX session management
# ---------------------------------------------------------------------------

def _get_df_session_path(session_id: str) -> str:
    return (
        f"projects/{PROJECT_ID}/locations/{DIALOGFLOW_LOCATION}/"
        f"agents/{AGENT_ID}/sessions/{session_id}"
    )


async def _detect_intent(
    session_id: str,
    text: str,
    language: str = "en",
) -> Dict[str, Any]:
    """
    Send recognized text to Dialogflow CX and return structured response.
    """
    language_code = _LANGUAGE_VOICES.get(language, _LANGUAGE_VOICES["en"])["language_code"]
    session_path = _get_df_session_path(session_id)

    request = dialogflow.DetectIntentRequest(
        session=session_path,
        query_input=dialogflow.QueryInput(
            text=dialogflow.TextInput(text=text),
            language_code=language_code,
        ),
    )
    response = _df_sessions_client.detect_intent(request=request)
    qr = response.query_result
    response_text = " ".join(
        msg.text.text[0]
        for msg in qr.response_messages
        if msg.text.text
    )
    return {
        "response_text": response_text,
        "parameters": dict(qr.parameters),
        "current_page": qr.current_page.display_name if qr.current_page else "",
        "session_id": session_id,
    }


# ---------------------------------------------------------------------------
# Speech-to-Text streaming config
# ---------------------------------------------------------------------------

def _build_streaming_config(language: str = "en") -> cloud_speech.StreamingRecognizeRequest:
    """Build the initial STT config for a streaming session."""
    language_code = _LANGUAGE_VOICES.get(language, _LANGUAGE_VOICES["en"])["language_code"]
    return cloud_speech.StreamingRecognizeRequest(
        recognizer=f"projects/{PROJECT_ID}/locations/global/recognizers/_",
        streaming_config=cloud_speech.StreamingRecognitionConfig(
            config=cloud_speech.RecognitionConfig(
                explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
                    encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.MULAW,
                    sample_rate_hertz=8000,
                    audio_channel_count=1,
                ),
                language_codes=[language_code, "en-US"],  # Always include English fallback
                model="telephony",
                features=cloud_speech.RecognitionFeatures(
                    enable_automatic_punctuation=True,
                    enable_word_time_offsets=False,
                    profanity_filter=False,
                    enable_spoken_emojis=False,
                ),
            ),
            streaming_features=cloud_speech.StreamingRecognitionFeatures(
                enable_voice_activity_events=True,
                interim_results=True,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Call session state
# ---------------------------------------------------------------------------

class CallSession:
    """Encapsulates state for a single active phone call."""

    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self.session_id = str(uuid.uuid4())
        self.language = "en"
        self.transcript: list[str] = []
        self.start_time = time.time()
        self.case_number: Optional[str] = None
        self.transfer_requested = False
        self.stt_client = SpeechAsyncClient()
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._response_queue: asyncio.Queue[str] = asyncio.Queue()
        self._last_speech_time = time.time()

    def note_speech(self) -> None:
        self._last_speech_time = time.time()

    def is_silence_timeout(self) -> bool:
        return (time.time() - self._last_speech_time) > SILENCE_TIMEOUT

    def log_transcript_turn(self, speaker: str, text: str) -> None:
        self.transcript.append(f"{speaker}: {text}")
        if LOG_TRANSCRIPTS:
            logger.info("[%s] %s: %s", self.call_sid[:8], speaker, text)


# Active sessions keyed by call_sid
_active_sessions: Dict[str, CallSession] = {}


# ---------------------------------------------------------------------------
# TwiML inbound endpoint
# ---------------------------------------------------------------------------

@app.post("/voice/inbound")
async def voice_inbound(request: Request) -> Response:
    """
    Twilio calls this endpoint when an inbound call arrives.
    Returns TwiML that connects the call to the Media Streams WebSocket.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    from_number = form.get("From", "")

    # Create session
    session = CallSession(call_sid)
    _active_sessions[call_sid] = session

    host = request.headers.get("host", request.base_url.hostname)
    ws_url = f"wss://{host}/voice/stream?call_sid={call_sid}"

    # Opening prompt audio will be sent over the WebSocket
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" track="inbound_track">
            <Parameter name="call_sid" value="{call_sid}"/>
        </Stream>
    </Connect>
</Response>"""

    logger.info("Inbound call: %s from %s", call_sid, from_number[:6] + "****")
    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# WebSocket Media Streams handler
# ---------------------------------------------------------------------------

@app.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket):
    """
    Bidirectional WebSocket:
    - Receives Twilio Media Streams audio (mu-law 8kHz base64-encoded)
    - Streams audio to Google STT in real time
    - Passes transcripts to Dialogflow CX
    - Streams TTS audio back to caller via Twilio
    """
    await websocket.accept()

    call_sid = websocket.query_params.get("call_sid", str(uuid.uuid4()))
    session = _active_sessions.get(call_sid) or CallSession(call_sid)
    _active_sessions[call_sid] = session

    logger.info("WebSocket connected for call %s", call_sid[:8])

    # Send opening prompt
    opening_audio = await _synthesize_speech(_OPENING_PROMPTS["en"], "en")
    await _send_audio_to_twilio(websocket, opening_audio, call_sid)
    session.log_transcript_turn("AGENT", _OPENING_PROMPTS["en"])

    # Start STT streaming task
    stt_task = asyncio.create_task(
        _stt_streaming_loop(session, websocket)
    )

    # Receive audio from Twilio
    try:
        while True:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            data = json.loads(raw)
            event = data.get("event")

            if event == "media":
                # Decode mu-law audio and push to STT queue
                payload = base64.b64decode(data["media"]["payload"])
                await session._audio_queue.put(payload)
                session.note_speech()

            elif event == "stop":
                logger.info("Twilio stream stopped for call %s", call_sid[:8])
                break

            elif event == "mark":
                pass  # Twilio playback position marker

    except (WebSocketDisconnect, asyncio.TimeoutError):
        logger.info("WebSocket disconnected for call %s", call_sid[:8])

    finally:
        stt_task.cancel()
        await _save_call_transcript(session)
        _active_sessions.pop(call_sid, None)
        try:
            await websocket.close()
        except Exception:
            pass


async def _stt_streaming_loop(session: CallSession, websocket: WebSocket) -> None:
    """
    Reads audio from session queue, streams to STT, passes transcripts to
    Dialogflow CX, synthesizes responses, and sends audio back to Twilio.
    """
    async def _audio_generator() -> AsyncGenerator[cloud_speech.StreamingRecognizeRequest, None]:
        yield _build_streaming_config(session.language)
        while True:
            try:
                chunk = await asyncio.wait_for(session._audio_queue.get(), timeout=1.0)
                yield cloud_speech.StreamingRecognizeRequest(audio=chunk)
            except asyncio.TimeoutError:
                continue

    try:
        async for response in await session.stt_client.streaming_recognize(
            requests=_audio_generator()
        ):
            for result in response.results:
                if not result.alternatives:
                    continue

                transcript = result.alternatives[0].transcript.strip()
                if not transcript:
                    continue

                is_final = result.is_final
                if not is_final:
                    continue  # Skip interim results — only process finals

                session.log_transcript_turn("CALLER", transcript)

                # Language switching detection
                detected_lang = _detect_language_switch(transcript)
                if detected_lang and detected_lang != session.language:
                    session.language = detected_lang
                    switch_msg = f"I'll switch to {_LANGUAGE_VOICES[detected_lang]['display']}."
                    audio = await _synthesize_speech(switch_msg, detected_lang)
                    await _send_audio_to_twilio(websocket, audio, session.call_sid)

                # Send to Dialogflow CX
                df_result = await _detect_intent(session.session_id, transcript, session.language)
                response_text = df_result.get("response_text", "")

                # Extract case number if mentioned
                if not session.case_number:
                    case_match = re.search(r"[A-Z]{2}-[A-Z]+-\d{4}-\d+", transcript)
                    if case_match:
                        session.case_number = case_match.group(0)

                # Check for transfer request
                params = df_result.get("parameters", {})
                if params.get("transfer_requested") or "transfer" in response_text.lower():
                    session.transfer_requested = True
                    await _initiate_transfer(websocket, session)
                    return

                if response_text:
                    session.log_transcript_turn("AGENT", response_text)
                    audio = await _synthesize_speech(response_text, session.language)
                    await _send_audio_to_twilio(websocket, audio, session.call_sid)

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("STT streaming loop error: %s", exc)
        error_msg = "I'm having technical difficulties. Please call 2-1-1 for immediate housing help."
        audio = await _synthesize_speech(error_msg, session.language)
        await _send_audio_to_twilio(websocket, audio, session.call_sid)


async def _send_audio_to_twilio(websocket: WebSocket, audio_bytes: bytes, call_sid: str) -> None:
    """Encode audio as base64 and send via Twilio Media Streams protocol."""
    message = json.dumps({
        "event": "media",
        "streamSid": call_sid,
        "media": {
            "payload": base64.b64encode(audio_bytes).decode("utf-8"),
        },
    })
    try:
        await websocket.send_text(message)
    except Exception as exc:
        logger.warning("Failed to send audio to Twilio: %s", exc)


async def _initiate_transfer(websocket: WebSocket, session: CallSession) -> None:
    """Warm transfer to live legal aid intake line via Twilio REST API."""
    transfer_msg = (
        "I'm connecting you to a housing specialist now. Please hold for a moment."
    )
    audio = await _synthesize_speech(transfer_msg, session.language)
    await _send_audio_to_twilio(websocket, audio, session.call_sid)

    # Log transfer request to Firestore
    if session.case_number:
        _firestore_client.collection("transfer_requests").document(session.call_sid).set({
            "call_sid": session.call_sid,
            "case_number": session.case_number,
            "transfer_number": TRANSFER_PHONE,
            "language": session.language,
            "requested_at": time.time(),
        })

    # Signal Twilio to redirect via REST API
    try:
        twilio_creds = _get_twilio_creds_sync()
        import requests
        from requests.auth import HTTPBasicAuth
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{twilio_creds['account_sid']}/Calls/{session.call_sid}.json"
        )
        twiml = f"<Response><Dial>{TRANSFER_PHONE}</Dial></Response>"
        requests.post(
            url,
            data={"Twiml": twiml},
            auth=HTTPBasicAuth(twilio_creds["account_sid"], twilio_creds["auth_token"]),
            timeout=10,
        )
    except Exception as exc:
        logger.error("Transfer via Twilio REST failed: %s", exc)


def _detect_language_switch(text: str) -> Optional[str]:
    """Detect explicit language switch requests."""
    lower = text.lower()
    if "español" in lower or "spanish" in lower:
        return "es"
    if "kreyòl" in lower or "creole" in lower or "haitian" in lower:
        return "ht"
    if "tiếng việt" in lower or "vietnamese" in lower:
        return "vi"
    if "english" in lower:
        return "en"
    return None


async def _save_call_transcript(session: CallSession) -> None:
    """Save call transcript and session metadata to Firestore."""
    if not LOG_TRANSCRIPTS:
        return
    call_duration = int(time.time() - session.start_time)
    doc = {
        "call_sid": session.call_sid,
        "session_id": session.session_id,
        "language": session.language,
        "case_number": session.case_number,
        "call_duration_seconds": call_duration,
        "transfer_requested": session.transfer_requested,
        "referral_outcome": "transfer" if session.transfer_requested else "information_only",
        "transcript": session.transcript,
        "created_at": time.time(),
    }
    _firestore_client.collection("call_transcripts").document(session.call_sid).set(doc)
    logger.info("Saved call transcript for %s (%ds)", session.call_sid[:8], call_duration)


def _get_twilio_creds_sync() -> Dict[str, str]:
    secret_path = f"projects/{PROJECT_ID}/secrets/{TWILIO_SECRET_NAME}/versions/latest"
    response = _secret_client.access_secret_version(name=secret_path)
    return json.loads(response.payload.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "service": "evictionshield-voice"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
