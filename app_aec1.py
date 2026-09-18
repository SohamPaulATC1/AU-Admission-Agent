import plivo
from quart import Quart, websocket, Response, request , send_from_directory , send_file
import asyncio
import json
import base64
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import os
import traceback
import contextlib
import logging
import audioop
import time
import wave
import sqlite3
from pyrnnoise import RNNoise
from aec import AcousticEchoCanceller
from call_runtime import (
    OutputResampler, PlivoPlayback, ResponseTracker, SpeechGate,
    cancel_tasks, soft_limit,
)
import numpy as np
import urllib.parse

# Import the new GenAI SDK
from google import genai
from google.genai import types

load_dotenv()

# --- Logging Setup ---
ist_tz = timezone(timedelta(hours=5, minutes=30))
logging.Formatter.converter = lambda *args: datetime.now(ist_tz).timetuple()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("Gemini_Assistant.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- PRICING CONSTANTS (Per 1M Tokens) ---
PRICE_TEXT_INPUT = 0.50
PRICE_TEXT_OUTPUT = 2.00
PRICE_AUDIO_INPUT = 3.00
PRICE_AUDIO_OUTPUT = 12.00

LIVE_API_KEY = os.getenv('GOOGLE_API_KEY')
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-live-extended-thinking")
#GEMINI_MODEL = "gemini-3.1-flash-live-preview"
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL')
HUMAN_TRANSFER_NUMBER = "+918335027643"
PLIVO_PHONE_NUMBER = os.getenv('FROM_NUMBER')

PORT = 8000

PLIVO_START_TIMEOUT_SECONDS = float(os.getenv("PLIVO_START_TIMEOUT_SECONDS", "10"))
GEMINI_CONNECT_TIMEOUT_SECONDS = float(os.getenv("GEMINI_CONNECT_TIMEOUT_SECONDS", "20"))
MODEL_RESPONSE_TIMEOUT_SECONDS = float(os.getenv("MODEL_RESPONSE_TIMEOUT_SECONDS", "30"))
MAX_CALL_DURATION_SECONDS = float(os.getenv("MAX_CALL_DURATION_SECONDS", "900"))
SILENCE_FOLLOWUP_SECONDS = float(os.getenv("SILENCE_FOLLOWUP_SECONDS", "8"))
MAX_SILENCE_FOLLOWUPS = int(os.getenv("MAX_SILENCE_FOLLOWUPS", "2"))
TERMINAL_ACTION_TIMEOUT_SECONDS = float(os.getenv("TERMINAL_ACTION_TIMEOUT_SECONDS", "8"))
MAX_GEMINI_RECONNECTS = int(os.getenv("MAX_GEMINI_RECONNECTS", "5"))
GEMINI_RECONNECT_DELAY_SECONDS = float(os.getenv("GEMINI_RECONNECT_DELAY_SECONDS", "1.0"))

TRANSFER_CONTEXT_DB_PATH = os.getenv("TRANSFER_CONTEXT_DB_PATH", "./transfer_context.sqlite3")
TRANSFER_CONTEXT_TTL_SECONDS = int(os.getenv("TRANSFER_CONTEXT_TTL_SECONDS", "86400"))
API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "default-dev-token")

# Telephony / model audio formats
PLIVO_SAMPLE_RATE = 8000
GEMINI_INPUT_RATE = 16000
GEMINI_OUTPUT_RATE = 24000

# Audio chunking
PLIVO_ULAW_CHUNK_SIZE = 160          # 20 ms @ 8kHz μ-law
GEMINI_PCM_CHUNK_SIZE = 640          # 20 ms @ 16kHz PCM16 = 320 samples = 640 bytes
PREROLL_MAX_BYTES_PCM8 = 3200        # ~200 ms @ 8kHz PCM16

VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.75"))
VAD_THRESHOLD_WHILE_SPEAKING = float(os.getenv("VAD_THRESHOLD_WHILE_SPEAKING", "0.92"))
VAD_ONSET_MS = max(20, int(os.getenv("VAD_ONSET_MS", "80")))
VAD_BARGE_IN_MS = max(VAD_ONSET_MS, int(os.getenv("VAD_BARGE_IN_MS", "240")))
VAD_MIN_RMS_DB = float(os.getenv("VAD_MIN_RMS_DB", "-50"))
VAD_BARGE_MIN_RMS_DB = float(os.getenv("VAD_BARGE_MIN_RMS_DB", "-42"))

PREROLL_MAX_BYTES_PCM16 = max(6400, (VAD_BARGE_IN_MS + 80) * 32)  # Preserve onset through longer barge-in confirmation.

# DSP Constants
AGC_TARGET_DB = -12.0
AGC_MAX_GAIN_DB = 15.0  # Same effective cap as the previous hardcoded clamp.
AGC_NOISE_GATE_DB = -50.0
AGC_SMOOTHING_ALPHA = 0.08         # Slow alpha to prevent volume pumping

# Hysteresis plus 400 ms of silence avoids splitting ordinary short pauses.
VAD_SILENCE_OFFSET_MS = max(20, int(os.getenv("VAD_SILENCE_OFFSET_MS", "400")))
PLAYBACK_ACK_TIMEOUT_SECONDS = float(os.getenv("PLAYBACK_ACK_TIMEOUT_SECONDS", "5"))
MAX_EMPTY_RESPONSE_RETRIES = 1
MAX_INPUT_QUEUE_FRAMES = 25  # At most 500 ms of 20 ms frames.
SSE_HEARTBEAT_SECONDS = 15

# The demo uses manual VAD and does not record call audio. Existing .env flags
# intentionally cannot enable the unvalidated hybrid path or debug WAV writes.
HYBRID_VAD = False
RECORD_GEMINI_INPUT = True

TERMINAL_ACTION_RECHECK_SECONDS = 0.2
LOG_EVERY_N_CHUNKS = 50
SUPERVISOR_POLL_INTERVAL = 0.25
PLIVO_SEND_POLL_TIMEOUT = 0.1
SOFT_LIMITER_THRESHOLD = 0.9
POST_TRANSFER_DELAY_SECONDS = 1.0

#total_cost = 0

class GeminiSessionDisconnected(Exception):
    """Raised when the Gemini Live session drops but may be resumable."""
    pass

with open("prompt1.txt", "r", encoding="utf-8") as f:
    RAW_SYSTEM_PROMPT = f.read()


def initialize_transfer_context_store():
    with sqlite3.connect(TRANSFER_CONTEXT_DB_PATH, timeout=5) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transfer_context (
                call_uuid TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                phone_number TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "DELETE FROM transfer_context WHERE created_at < ?",
            (time.time() - TRANSFER_CONTEXT_TTL_SECONDS,),
        )


def save_transfer_context(call_uuid, summary, phone_number):
    with sqlite3.connect(TRANSFER_CONTEXT_DB_PATH, timeout=5) as conn:
        conn.execute(
            """
            INSERT INTO transfer_context (call_uuid, summary, phone_number, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(call_uuid) DO UPDATE SET
                summary = excluded.summary,
                phone_number = excluded.phone_number,
                created_at = excluded.created_at
            """,
            (call_uuid, summary, phone_number, time.time()),
        )


def load_transfer_context(call_uuid):
    if not call_uuid:
        return {}

    with sqlite3.connect(TRANSFER_CONTEXT_DB_PATH, timeout=5) as conn:
        row = conn.execute(
            """
            SELECT summary, phone_number
            FROM transfer_context
            WHERE call_uuid = ? AND created_at >= ?
            """,
            (call_uuid, time.time() - TRANSFER_CONTEXT_TTL_SECONDS),
        ).fetchone()

    if not row:
        return {}

    return {"summary": row[0], "phone_number": row[1]}


initialize_transfer_context_store()

def get_indian_time():
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    return now_ist.strftime("%A, %d %B %Y %H:%M:%S IST")

# =============================================================================
# Audio utilities
# =============================================================================

_ULAW_DECODE_TABLE = np.array(
    [
        -32124, -31100, -30076, -29052, -28028, -27004, -25980, -24956,
        -23932, -22908, -21884, -20860, -19836, -18812, -17788, -16764,
        -15996, -15484, -14972, -14460, -13948, -13436, -12924, -12412,
        -11900, -11388, -10876, -10364, -9852, -9340, -8828, -8316,
        -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
        -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
        -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
        -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
        -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
        -1372, -1308, -1244, -1180, -1116, -1052, -988, -924,
        -876, -844, -812, -780, -748, -716, -684, -652,
        -620, -588, -556, -524, -492, -460, -428, -396,
        -372, -356, -340, -324, -308, -292, -276, -260,
        -244, -228, -212, -196, -180, -164, -148, -132,
        -120, -112, -104, -96, -88, -80, -72, -64,
        -56, -48, -40, -32, -24, -16, -8, 0,
        32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
        23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
        15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
        11900, 11388, 10876, 10364, 9852, 9340, 8828, 8316,
        7932, 7676, 7420, 7164, 6908, 6652, 6396, 6140,
        5884, 5628, 5372, 5116, 4860, 4604, 4348, 4092,
        3900, 3772, 3644, 3516, 3388, 3260, 3132, 3004,
        2876, 2748, 2620, 2492, 2364, 2236, 2108, 1980,
        1884, 1820, 1756, 1692, 1628, 1564, 1500, 1436,
        1372, 1308, 1244, 1180, 1116, 1052, 988, 924,
        876, 844, 812, 780, 748, 716, 684, 652,
        620, 588, 556, 524, 492, 460, 428, 396,
        372, 356, 340, 324, 308, 292, 276, 260,
        244, 228, 212, 196, 180, 164, 148, 132,
        120, 112, 104, 96, 88, 80, 72, 64,
        56, 48, 40, 32, 24, 16, 8, 0,
    ],
    dtype=np.int16,
)


def ulaw_to_pcm(ulaw_data: bytes) -> bytes:
    ulaw_samples = np.frombuffer(ulaw_data, dtype=np.uint8)
    pcm_samples = _ULAW_DECODE_TABLE[ulaw_samples]
    return pcm_samples.tobytes()


def pcm_to_ulaw(pcm_data: bytes) -> bytes:
    BIAS = 0x84
    CLIP = 32635

    pcm_samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.int32)
    sign = (pcm_samples >> 8) & 0x80
    pcm_samples = np.where(sign != 0, -pcm_samples, pcm_samples)
    pcm_samples = np.clip(pcm_samples, 0, CLIP) + BIAS

    segment = np.floor(np.log2(np.maximum(pcm_samples >> 7, 1))).astype(np.int32)
    segment = np.clip(segment, 0, 7)

    ulaw = sign | ((segment << 4) | ((pcm_samples >> (segment + 3)) & 0x0F))
    ulaw = ~ulaw & 0xFF

    return ulaw.astype(np.uint8).tobytes()

DISCLAIMER_ULAW_CHUNKS = []

def load_disclaimer():
    try:
        with wave.open("playback_audio_files/recorded1.wav", "rb") as wf:
            pcm_data = wf.readframes(wf.getnframes())

            ulaw_data = pcm_to_ulaw(pcm_data)

            chunk_size = 160  # 20ms of 8kHz mu-law
            for i in range(0, len(ulaw_data), chunk_size):
                chunk = ulaw_data[i:i+chunk_size]
                if len(chunk) == chunk_size:
                    DISCLAIMER_ULAW_CHUNKS.append(base64.b64encode(chunk).decode("utf-8"))

        logger.info(f"✅ Loaded pre-optimized recorded1.wav ({len(DISCLAIMER_ULAW_CHUNKS)} chunks).")
    except Exception as e:
        logger.warning(f"⚠️ Could not load recorded1.wav. Disclaimer disabled. Error: {e}")

load_disclaimer()

def format_digits(number_str):
    digit_words = {
        '0': 'zero', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
        '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine'
    }
    return "-".join(digit_words[d] for d in number_str.strip())

# Define native Python functions for the tools
def execute_end_call():
    return json.dumps({"action": "END_CALL", "statusCode": 200})

def execute_transfer_call(summary: str):
    return json.dumps({"action": "TRANSFER_CALL", "statusCode": 200, "summary": summary})

# Define the Function Declarations for the Gemini API
end_call_tool = types.FunctionDeclaration(
    name="endCall",
    description="Ends the call when the user says goodbye or wants to end the conversation.",
    # NON_BLOCKING is required by gemini-3.8-live-extended-thinking (blocking
    # function calls return a hard error on that model). The deferred-terminal
    # dispatch below already returns an "accepted" FunctionResponse without a
    # scheduling field, which is exactly the non-blocking contract.
    behavior=types.Behavior.NON_BLOCKING,
    parameters={
        "type": "OBJECT",
        "properties": {
            "summary_of_whole_call": {
                "type": "STRING",
                "description": (
                    "A thorough, complete English summary of the ENTIRE admission call for the admission team. "
                    "Cover: whether you spoke with the student or a parent/guardian, the applicant's name if known, "
                    "highest qualification, board/university, the exact program(s) of interest, the questions the user "
                    "asked, the information you shared (fees/eligibility given), their level of interest and intent, "
                    "any callback preference or concern raised, and the overall outcome of the call. Write several "
                    "sentences — do not compress it to one line."
                )
            }
        },
        "required": ["summary_of_whole_call"]
    }
)

transfer_call_tool = types.FunctionDeclaration(
    name="transferCall",
    description=(
        "Hands the call over to a senior human admission counselor when the user asks to speak with a person "
        "or asks something beyond the AI agent's knowledge that needs a counselor."
    ),
    # NON_BLOCKING required by gemini-3.8-live-extended-thinking (see endCall).
    behavior=types.Behavior.NON_BLOCKING,
    parameters={
        "type": "OBJECT",
        "properties": {
            "call_summary": {
                "type": "STRING",
                "description": "A concise English summary of the entire conversation covering: what the user's issue was, what was resolved, what the human agent needs to handle, and your overall opinion of the call."
            },
            "language": {
                "type": "STRING",
                "description": "The language in which the user was speaking (e.g., 'english', 'bengali', 'hindi') so the human agent can pick up in the desired language."
            }
        },
        "required": ["call_summary", "language"]
    }
)

LOCAL_GEMINI_TOOLS = [{"function_declarations": [end_call_tool, transfer_call_tool]}]

SILENCE_FOLLOWUP_PROMPT = (
    "The user has been silent for a few seconds after you spoke with them last."
    "Please briefly and politely re-engage them in the same language you have been speaking. "
    "Keep it short, natural, non-pushy, and context-aware."
)

SILENCE_FAREWELL_PROMPT = (
    "The user has been completely unresponsive after multiple follow-ups. "
    "Please say a brief, polite farewell in the language you have been speaking "
    "(e.g., 'It seems you are busy right now. I will end the call. Thank you and have a nice day!') "
    "and then immediately call the endCall tool with a summary of the conversation."
)

async def silence_watchdog(session, call_state, delay=SILENCE_FOLLOWUP_SECONDS):
    epoch = call_state['responses'].epoch
    try:
        await asyncio.sleep(delay)
        if (
            call_state.get('terminate_session') or not call_state.get('session_ready')
            or call_state['responses'].epoch != epoch
            or call_state['user_activity_open'] or call_state['is_speaking']
            or call_state['playback'].busy or call_state['responses'].active is not None
        ):
            return
        if call_state['silence_followup_count'] >= MAX_SILENCE_FOLLOWUPS:
            call_state['closing_audio_phase'] = True
            call_state['terminal_action_deadline'] = time.monotonic() + MODEL_RESPONSE_TIMEOUT_SECONDS
            await send_model_prompt(session, call_state, SILENCE_FAREWELL_PROMPT, 'farewell')
        else:
            call_state['silence_followup_count'] += 1
            await send_model_prompt(session, call_state, SILENCE_FOLLOWUP_PROMPT, 'followup')
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The session supervisor owns recovery; a timer must not terminate a
        # healthy successor session after the connection it used has expired.
        if call_state['responses'].epoch == epoch:
            call_state['session_error'] = exc


def calculate_rms_db(pcm_data):
    """Converts int16 PCM to float32, calculates RMS in dB, and returns both."""
    samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
    rms_val = np.sqrt(np.mean(samples**2) + 1e-12)
    rms_db = 20 * np.log10(rms_val + 1e-12)
    return rms_db, samples

app = Quart(__name__)

# --- SSE (Server-Sent Events) Infrastructure for Dashboard ---
call_event_subscribers = set()

def emit_call_event(call_id, event_type, data=None):
    """Fire-and-forget: push a JSON event to all connected dashboard browsers."""
    global call_event_subscribers
    try:
        payload = json.dumps({
            "call_id": str(call_id) if call_id else "",
            "type": event_type,
            "data": data or {},
            "timestamp": datetime.now(ist_tz).strftime("%H:%M:%S"),
        })
        dead = set()
        for q in call_event_subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.add(q)
        call_event_subscribers -= dead
    except Exception as e:
        logger.error(f"SSE Emit Error: {e}")
        pass  # Never let SSE emission interfere with call processing

@app.route('/call-events')
async def call_events_stream():
    """SSE endpoint — the dashboard subscribes to this for real-time call events."""
    q = asyncio.Queue(maxsize=200)
    call_event_subscribers.add(q)

    async def event_generator():
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            call_event_subscribers.discard(q)

    response = Response(
        event_generator(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )
    response.timeout = None
    return response

plivo_client = plivo.RestClient(auth_id=os.getenv('PLIVO_AUTH_ID'), auth_token=os.getenv('PLIVO_AUTH_TOKEN'))

@app.route('/playback_audio_files/<path:filename>')
async def serve_audio(filename):
    """Serves audio files from the local audio_files directory."""
    logger.info(f"Serving audio file: {filename}")
    return await send_from_directory('playback_audio_files', filename)

@app.route('/senco.png', methods=['GET'])
async def serve_senco_logo():
    return await send_file('senco.png')

@app.route('/favicon.png', methods=['GET'])
async def serve_favi_logo():
    return await send_file('favicon.png')

@app.route('/atc.png', methods=['GET'])
async def serve_atc_logo():
    return await send_file('atc.png')

@app.route("/", methods=["GET"])
async def serve_dashboard():
    """Serves the frontend dashboard."""
    logger.info("Serving the dashboard UI")
    return await send_file("index.html")

# FOR INCOMING CALL

# @app.route("/webhook", methods=["GET", "POST"])
# async def home():
#     if request.method == "POST":
#         form_data = await request.form
#         from_number = form_data.get("From", "Unknown")
#     else:
#         from_number = request.args.get("From", "Unknown")

#     from_number = str(from_number)[2:]

#     logger.info(f"Incoming call webhook triggered by: {from_number}")

#     AUDIO_URL = f"{PUBLIC_BASE_URL}/playback_audio_files/recorded.wav"

#     xml_data = f'''<?xml version="1.0" encoding="UTF-8"?>
#     <Response>
# #         <Play>{AUDIO_URL}</Play>
#         <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" noiseCancellation="true" noiseCancellationLevel="85" contentType="audio/x-mulaw;rate=8000" audioTrack="inbound" >
#             wss://{request.host}/media-stream?from_number={from_number}
#         </Stream>
#     </Response>
#     '''
#     return Response(xml_data, mimetype='application/xml')

@app.route("/outbound-webhook", methods=["GET", "POST"])
async def outbound_webhook():
    # Extract user details passed from the main block
    user_name = request.args.get("user_name", "Unknown")
    phone_number = request.args.get("phone_number", "Unknown")

    logger.info(f"📞 Outbound call answered by: {phone_number} ({user_name})")

    import urllib.parse
    encoded_name = urllib.parse.quote(user_name)
    encoded_phone = urllib.parse.quote(phone_number)

    ws_base_url = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    xml_data = f'''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" noiseCancellation="true" noiseCancellationLevel="85" contentType="audio/x-mulaw;rate=8000" audioTrack="inbound" >
            {ws_base_url}/media-stream?user_name={encoded_name}&amp;phone_number={encoded_phone}
        </Stream>
    </Response>
    '''
    return Response(xml_data, mimetype='application/xml')

@app.route("/hangup", methods=["GET", "POST"])
def hangup():
    xml_data = '''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Hangup/>
    </Response>
    '''
    return Response(xml_data, mimetype='application/xml')

@app.route('/transfer.xml', methods=['GET', 'POST'])
async def transfer_xml():
    logger.info("Here in transfer xml method")

    if request.method == 'POST':
        form_data = await request.form

    plivo_number = PLIVO_PHONE_NUMBER

    target_number = request.args.get("number", HUMAN_TRANSFER_NUMBER)
    call_uuid = request.args.get("call_uuid", "")

    logger.info(f"Serving transfer XML — call_uuid={call_uuid}, target={target_number}, from={plivo_number}")

    import urllib.parse
    dial_action_url = f"{PUBLIC_BASE_URL}/dial-action?call_uuid={urllib.parse.quote(call_uuid)}"

    HUMAN_TRANSFER_AUDIO_URL = f"{PUBLIC_BASE_URL}/playback_audio_files/transfer.wav"
    NO_AGENT_AUDIO_URL = f"{PUBLIC_BASE_URL}/playback_audio_files/no_agent.wav"
    xml_data = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Play>{HUMAN_TRANSFER_AUDIO_URL}</Play>
        <Dial callerId="{plivo_number}" timeout="60" action="{dial_action_url}" method="POST">
            <Number>{target_number}</Number>
        </Dial>
        <Play>{NO_AGENT_AUDIO_URL}</Play>
        <Hangup/>
    </Response>
    """
    return Response(xml_data, mimetype='application/xml')

@app.route('/dial-action', methods=['GET', 'POST'])
async def dial_action():
    """
    Plivo calls this when the Dial leg ends (human hangs up, no-answer, busy, failed).
    We retrieve the stored context and restart the AI stream with a custom prompt.
    """
    logger.info("📲 Dial action callback received")

    if request.method == 'POST':
        form_data = await request.form
        # Plivo uses DialBLegStatus. Twilio uses DialCallStatus. We check all to be safe.
        dial_status = form_data.get('DialBLegStatus') or form_data.get('DialCallStatus') or form_data.get('DialStatus', 'unknown')
        from_number = form_data.get('From', 'Unknown')
        plivo_call_uuid = form_data.get('CallUUID', '')
    else:
        dial_status = request.args.get('DialBLegStatus') or request.args.get('DialCallStatus') or request.args.get('DialStatus', 'unknown')
        from_number = request.args.get('From', 'Unknown')
        plivo_call_uuid = request.args.get('CallUUID', '')

    # Safely get call_uuid from URL args first, fallback to form data
    call_uuid = request.args.get("call_uuid") or plivo_call_uuid

    # Normalize status (Plivo sends "hangup" when the human ends an answered call)
    dial_status = dial_status.lower()
    logger.info(f"📲 DialStatus={dial_status}, call_uuid={call_uuid}")

    context = await asyncio.to_thread(load_transfer_context, call_uuid)
    if not context:
        logger.warning(f"⚠️ No transfer context found for call_uuid={call_uuid}! Proceeding with empty context.")

    summary = context.get("summary", "")
    original_from = context.get("phone_number", from_number)

    logger.info(f"📋 Retrieved summary for re-entry: {summary}")

    encoded_phone = urllib.parse.quote(original_from)
    encoded_context_id = urllib.parse.quote(call_uuid or "")

    # Map Plivo's successful states to 'completed' so handle_media_stream reads it correctly
    is_success = "completed" if dial_status in ["hangup", "answered", "completed"] else "failed"
    encoded_status   = urllib.parse.quote(is_success)

    # Strip protocol from PUBLIC_BASE_URL to build wss:// URL
    ws_host = PUBLIC_BASE_URL.replace("https://", "").replace("http://", "")

    RECONNECTING_URL = f"{PUBLIC_BASE_URL}/playback_audio_files/reconnecting.wav"
    xml_data = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Play>{RECONNECTING_URL}</Play>
        <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true"
                noiseCancellation="true" noiseCancellationLevel="85"
                contentType="audio/x-mulaw;rate=8000" audioTrack="inbound">
            wss://{ws_host}/media-stream?phone_number={encoded_phone}&amp;transfer_context_id={encoded_context_id}&amp;dial_status={encoded_status}&amp;is_transfer_return=1
        </Stream>
    </Response>
    """
    return Response(xml_data, mimetype='application/xml')

def apply_plivo_start(call_state, start_data):
    logger.info('Plivo Audio stream has started')
    logger.info(f"Plivo start payload: {json.dumps(start_data)}")

    call_state["stream_id"] = start_data.get("streamId")
    call_state["call_uuid"] = (
        start_data.get('callId')
        or start_data.get('callUuid')
        or start_data.get('callUUID')
        or start_data.get('call_id')
        or start_data.get('call_uuid')
    )
    call_state["host"] = (
        start_data.get('customParameters', {}).get('host')
        or os.getenv("PUBLIC_HOST")
    )
    call_state["call_deadline"] = time.monotonic() + MAX_CALL_DURATION_SECONDS

    logger.info(f'Stream ID: {call_state["stream_id"]}')
    logger.info(f'Call UUID: {call_state["call_uuid"]}')
    logger.info(f'From Number: {call_state["from_number"]}')
    logger.info(f'PUBLIC_BASE_URL: {call_state["public_base_url"]}')
    emit_call_event(call_state.get("call_uuid"), "call_ringing", {"phone": call_state.get("from_number", "")})


async def wait_for_plivo_start(plivo_ws, call_state):
    async def receive_start():
        while True:
            message = await plivo_ws.receive()
            if message is None:
                raise ConnectionError("Plivo WebSocket closed before the start event")

            data = json.loads(message)
            event = data.get("event")
            if event == "start":
                apply_plivo_start(call_state, data.get("start", {}))
                return
            if event == "stop":
                raise ConnectionError("Plivo stopped the stream before the start event")

    await asyncio.wait_for(receive_start(), timeout=PLIVO_START_TIMEOUT_SECONDS)


LEADS_DIR = os.getenv("LEADS_DIR", "./leads")


def _safe_filename_part(value):
    """Make a string safe for use in a filename."""
    keep = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(value).strip())
    return keep.strip("_") or "unknown"


def save_lead(call_state, outcome, summary=None):
    """Persist an admission lead as JSON keyed by phone + name.

    Idempotent per call: sets call_state['lead_saved'] so the teardown
    fallback never overwrites a lead already written by endCall/transfer.
    The summary is the model-generated call summary — we never build it
    from the (unreliable, non-English) speech transcript buffers.
    """
    try:
        os.makedirs(LEADS_DIR, exist_ok=True)
        name = call_state.get("user_name", "Unknown")
        phone = call_state.get("from_number", "Unknown")
        if summary is None:
            summary = call_state.get("end_call_summary", "")
        record = {
            "name": name,
            "phone_number": phone,
            "outcome": outcome,
            "summary": summary,
            "call_uuid": call_state.get("call_uuid"),
            "timestamp": datetime.now(ist_tz).isoformat(),
        }
        filename = f"{_safe_filename_part(phone)}_{_safe_filename_part(name)}.json"
        path = os.path.join(LEADS_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        call_state["lead_saved"] = True
        logger.info(f"📝 Lead saved ({outcome}): {path}")
    except Exception as e:
        logger.error(f"❌ Failed to save lead: {e}")


async def terminate_plivo_call(plivo_client, call_state, reason):
    if call_state.get("hangup_started"):
        call_state["terminate_session"] = True
        return

    call_state["hangup_started"] = True
    call_uuid = call_state.get("call_uuid")
    logger.warning(f"Terminating call: {reason}")

    try:
        if call_uuid:
            await asyncio.wait_for(asyncio.to_thread(
                plivo_client.calls.delete,
                call_uuid=call_uuid,
            ), 5)
            logger.info(f"Plivo call explicitly terminated for call_uuid={call_uuid}")
        else:
            logger.warning("Cannot explicitly terminate Plivo call because call_uuid is unavailable")
    except Exception as e:
        logger.error(f"Failed to terminate Plivo call: {e}")
    finally:
        call_state["terminal_action_completed"] = True
        call_state["terminate_session"] = True


async def supervise_call(call_state, plivo_client):
    while not call_state.get('terminate_session'):
        now = time.monotonic()
        call_state['playback'].check_deadlines(now)
        if now >= call_state['call_deadline']:
            await terminate_plivo_call(plivo_client, call_state, 'maximum call duration reached')
            return
        if call_state['closing_audio_phase']:
            deadline = call_state.get('terminal_action_deadline')
            if (
                deadline is not None and now >= deadline
                and call_state['responses'].active is None
                and not call_state['pending_end_call'] and not call_state['pending_transfer_call']
            ):
                call_state['pending_end_call'] = True
                call_state['turn_complete'] = True
        if not call_state['playing_disclaimer']:
            if await execute_pending_terminal_action(None, call_state, plivo_client, None):
                return
        await asyncio.sleep(SUPERVISOR_POLL_INTERVAL)


@contextlib.asynccontextmanager
async def connect_live_with_timeout(client, model, config):
    live_context = client.aio.live.connect(model=model, config=config)
    session = await asyncio.wait_for(live_context.__aenter__(), GEMINI_CONNECT_TIMEOUT_SECONDS)
    try:
        yield session
    finally:
        # Cleanup must never swallow/replace the original failure or cancellation.
        try:
            await asyncio.wait_for(live_context.__aexit__(None, None, None), 5)
        except Exception as exc:
            logger.warning('Gemini connection cleanup: %s', exc)


async def coordinate_call_tasks(task_map, call_state):
    try:
        done, _ = await asyncio.wait(task_map.values(), return_when=asyncio.FIRST_COMPLETED)
        # Retrieve exceptions in stable task order. The finally retrieves the rest.
        for task in task_map.values():
            if task in done:
                task.result()
        return {name for name, task in task_map.items() if task in done}
    finally:
        call_state['terminate_session'] = True
        await cancel_tasks(task_map.values())


def log_call_stats(call_state):
    cost_text_in = (call_state["tokens_text_in"] / 1_000_000) * PRICE_TEXT_INPUT
    cost_text_out = (call_state["tokens_text_out"] / 1_000_000) * PRICE_TEXT_OUTPUT
    cost_audio_in = (call_state["tokens_audio_in"] / 1_000_000) * PRICE_AUDIO_INPUT
    cost_audio_out = (call_state["tokens_audio_out"] / 1_000_000) * PRICE_AUDIO_OUTPUT
    total_cost = cost_text_in + cost_text_out + cost_audio_in + cost_audio_out

    logger.info("=== CALL ENDED : STATS & PRICING ===")
    logger.info(f"Tokens Used - Text In: {call_state['tokens_text_in']}, Audio In: {call_state['tokens_audio_in']}, Text Out: {call_state['tokens_text_out']}, Audio Out: {call_state['tokens_audio_out']}")
    logger.info(f"Last usage_metadata.total_token_count seen: {call_state['last_usage_total_token_count']}")
    logger.info(f"usage_metadata.total_token_count updates: {call_state['usage_total_updates']}, non-monotonic transitions: {call_state['usage_total_non_monotonic_count']}")
    logger.info(f"Total Call Cost: ${total_cost:.6f}")
    logger.info("====================================")
    emit_call_event(call_state.get("call_uuid"), "call_ended", {
        "reason": call_state.get("end_call_summary", "call completed"),
        "cost": f"${total_cost:.4f}",
        "phone": call_state.get("from_number", ""),
    })


async def play_disclaimer(plivo_ws, call_state, disclaimer_finished_event):
    try:
        if DISCLAIMER_ULAW_CHUNKS and not call_state['is_returning_from_transfer']:
            audio = b''.join(base64.b64decode(chunk) for chunk in DISCLAIMER_ULAW_CHUNKS)
            call_state['playback'].feed('disclaimer', audio)
            await call_state['playback'].finish('disclaimer', 'disclaimer')
        call_state['playing_disclaimer'] = False
        disclaimer_finished_event.set()
    except asyncio.CancelledError:
        raise


def reset_input_state(call_state):
    call_state['is_speaking'] = False
    call_state['speech_gate'].reset()
    call_state['user_activity_open'] = False
    call_state['preroll_pcm16'].clear()
    call_state['gemini_input_buffer'].clear()
    call_state['ratecv_state_up'] = None
    call_state['ratecv_state_down'] = None
    call_state['agc_current_gain_lin'] = 1.0
    call_state['denoiser'] = RNNoise(sample_rate=48000)
    queue = call_state['input_audio_queue']
    while not queue.empty():
        queue.get_nowait()


def cancel_silence_timer(call_state):
    task = call_state.get('silence_timer_task')
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()
    call_state['silence_timer_task'] = None


def arm_silence_timer(call_state):
    cancel_silence_timer(call_state)
    if (
        call_state.get('session_ready') and call_state.get('greeting_completed')
        and not call_state['playing_disclaimer'] and not call_state['closing_audio_phase']
        and not call_state['user_activity_open'] and not call_state['is_speaking']
        and not call_state['playback'].busy and call_state['responses'].active is None
    ):
        task = asyncio.create_task(silence_watchdog(call_state['session'], call_state))
        call_state['silence_timer_task'] = task
        call_state['background_tasks'].add(task)
        task.add_done_callback(call_state['background_tasks'].discard)


async def playback_completed(call_state, mark):
    if mark.tag == 'disclaimer':
        call_state['playing_disclaimer'] = False
        return
    # Marks invalidated by a clear or reconnect never reach this callback.
    call_state['assistant_speaking'] = call_state['playback'].busy
    emit_call_event(call_state.get('call_uuid'), 'ai_done')
    logger.info('Playback acknowledged: call=%s mark=%s owner=%s',
                call_state.get('call_uuid'), mark.name, mark.owner)
    if mark.tag == 'greeting' and not call_state['greeting_completed']:
        reset_input_state(call_state)
        call_state['greeting_completed'] = True
        emit_call_event(call_state.get('call_uuid'), 'call_connected')
        logger.info('Initial greeting acknowledged. Microphone is now live.')
    arm_silence_timer(call_state)


async def send_model_prompt(session, call_state, text, kind, retries=0):
    cancel_silence_timer(call_state)
    turn = call_state['responses'].begin(kind, retries)
    call_state['turn_complete'] = False
    await asyncio.wait_for(session.send_client_content(
        turns=types.Content(role='user', parts=[types.Part.from_text(text=text)]),
        turn_complete=True,
    ), timeout=GEMINI_CONNECT_TIMEOUT_SECONDS)
    return turn


async def read_plivo_events(plivo_ws, call_state):
    """Only socket reader. It outlives individual Gemini connections."""
    remainder = bytearray()
    input_epoch = call_state['responses'].epoch
    try:
        while not call_state['terminate_session']:
            raw = await plivo_ws.receive()
            if raw is None:
                break
            data = json.loads(raw)
            event = data.get('event')
            if event == 'stop':
                break
            if event in ('playedStream', 'clearedAudio'):
                was_muted = not call_state['greeting_completed']
                await call_state['playback'].acknowledge(data)
                if was_muted and call_state['greeting_completed']:
                    remainder.clear()
                continue
            if event != 'media':
                continue
            media = data.get('media', {})
            if media.get('track', 'inbound') != 'inbound' or not media.get('payload'):
                continue
            if input_epoch != call_state['responses'].epoch:
                remainder.clear()
                input_epoch = call_state['responses'].epoch
            remainder.extend(base64.b64decode(media['payload'], validate=True))
            while len(remainder) >= PLIVO_ULAW_CHUNK_SIZE:
                frame = bytes(remainder[:PLIVO_ULAW_CHUNK_SIZE])
                del remainder[:PLIVO_ULAW_CHUNK_SIZE]
                # Consume far-end references even when input is muted. This
                # keeps the AEC clock moving through the disclaimer/greeting.
                pcm = call_state['aec'].process(ulaw_to_pcm(frame))
                if (
                    call_state['playing_disclaimer'] or not call_state['greeting_completed']
                    or not call_state.get('session_ready') or call_state['closing_audio_phase']
                ):
                    continue
                queue = call_state['input_audio_queue']
                if queue.full():
                    # Never replay a growing backlog of caller speech after a
                    # slow model send. Reconnect rather than silently splice a turn.
                    call_state['session_error'] = GeminiSessionDisconnected('Inbound audio backpressure')
                    while not queue.empty():
                        queue.get_nowait()
                queue.put_nowait((call_state['responses'].epoch, time.monotonic(), pcm))
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        raise
    except Exception:
        call_state['plivo_disconnected'] = True
        raise
    call_state['plivo_disconnected'] = True
    call_state['terminate_session'] = True


async def supervise_model_session(call_state, epoch):
    while call_state['responses'].epoch == epoch:
        if call_state['terminate_session']:
            return
        if call_state.get('session_error') is not None:
            raise call_state['session_error']
        now = time.monotonic()
        if call_state.get('go_away_received') and (
            (call_state['responses'].active is None and not call_state['playback'].busy
             and not call_state['user_activity_open'])
            or now >= call_state['go_away_deadline']
        ):
            raise GeminiSessionDisconnected('Gemini GoAway handover')
        if call_state['responses'].expired(now):
            raise GeminiSessionDisconnected('Gemini response/generation timeout')
        await asyncio.sleep(SUPERVISOR_POLL_INTERVAL)


def recoverable_gemini_error(exc):
    from google.genai import errors as genai_errors
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from httpx import TransportError
    if isinstance(exc, (GeminiSessionDisconnected, TimeoutError, ConnectionError, OSError, TransportError)):
        return True
    if isinstance(exc, ConnectionClosed):
        close = exc.rcvd or exc.sent
        return close is None or close.code in (1000, 1001, 1006, 1011, 1012, 1013)
    if isinstance(exc, InvalidStatus):
        return exc.response.status_code in (408, 429, 500, 502, 503, 504)
    if isinstance(exc, genai_errors.APIError):
        # Configuration/auth/policy failures (including 1008) aren't transient.
        return exc.code in (408, 429, 500, 502, 503, 504, 1000, 1001, 1006, 1011, 1012, 1013)
    return False


async def coordinate_model_tasks(tasks):
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if task in done:
                task.result()
    finally:
        await cancel_tasks(tasks)


async def recover_empty_response(session, call_state, turn):
    if turn.stale or call_state['user_activity_open'] or call_state['responses'].active is not None:
        return
    if turn.retries >= MAX_EMPTY_RESPONSE_RETRIES:
        call_state['pending_end_call'] = True
        call_state['closing_audio_phase'] = True
        call_state['turn_complete'] = True
        call_state['terminal_action_deadline'] = time.monotonic()
        logger.warning('Ending call after repeated empty Gemini responses: %s', turn.key)
        return
    if not call_state['greeting_completed']:
        text = call_state['initial_prompt']
        kind = 'greeting'
    elif call_state['closing_audio_phase']:
        text = SILENCE_FAREWELL_PROMPT
        kind = 'farewell'
    else:
        text = ('Your last turn contained no audible response. Briefly ask the caller '
                'to repeat their last question in their chosen language. Do not invent an answer.')
        kind = 'empty_retry'
    await send_model_prompt(session, call_state, text, kind, retries=turn.retries + 1)


async def run_gemini_sessions(client, call_state, system_instruction_text, initial_prompt, disclaimer_finished):
    for attempt in range(MAX_GEMINI_RECONNECTS + 1):
        if call_state['terminate_session']:
            return
        call_state['gemini_reconnect_count'] = attempt
        call_state['session_ready'] = False
        if attempt:
            if not call_state['playing_disclaimer']:
                await call_state['playback'].clear(reason='gemini-reconnect')
            emit_call_event(call_state.get('call_uuid'), 'gemini_reconnecting', {'attempt': attempt})
            await asyncio.sleep(min(GEMINI_RECONNECT_DELAY_SECONDS * 2 ** (attempt - 1), 4))
        if ('extended-thinking' in GEMINI_MODEL
                and 'interaction_status' not in types.LiveServerContent.model_fields):
            raise RuntimeError('Gemini Extended Thinking requires the SDK pinned in requirements.txt; '
                               'install project dependencies and restart the app.')
        config = types.LiveConnectConfig(
            system_instruction=types.Content(parts=[
                types.Part.from_text(text=system_instruction_text)
            ]),
            tools=LOCAL_GEMINI_TOOLS,
            temperature=0.2,
            session_resumption=types.SessionResumptionConfig(
                handle=call_state.get("session_resumption_handle")
            ),
            response_modalities=["AUDIO"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            thinking_config=types.ThinkingConfig(
                thinking_level="medium",
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Kore"
                    )
                )
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            ),
            context_window_compression=(
                types.ContextWindowCompressionConfig(
                    sliding_window=types.SlidingWindow(),
                )
            )
        )

        try:
            async with connect_live_with_timeout(client, GEMINI_MODEL, config) as session:
                epoch = call_state['responses'].new_session()
                reset_input_state(call_state)
                call_state['session_error'] = None
                call_state['session'] = session
                call_state['go_away_received'] = False
                call_state['turn_complete'] = False

                async def send_initial_and_listen():
                    await disclaimer_finished.wait()
                    if not call_state['greeting_completed']:
                        prompt, kind = initial_prompt, 'greeting'
                    else:
                        prompt = ('The audio connection was interrupted. Briefly ask the caller to '
                                  'repeat their last question in their chosen language. Do not '
                                  'restart the introduction or invent anything said during the gap.')
                        if not call_state.get('session_resumption_handle'):
                            history = json.dumps(call_state['conversation_log'][-12:], ensure_ascii=False)
                            prompt += '\nEarlier call context:\n' + initial_prompt + '\nTranscript:\n' + history[-12000:]
                        kind = 'recovery'
                    call_state['session_ready'] = True
                    await send_model_prompt(session, call_state, prompt, kind)
                    if attempt:
                        emit_call_event(call_state.get('call_uuid'), 'gemini_reconnected')
                    await stream_plivo_to_gemini(None, session, call_state)

                tasks = [
                    asyncio.create_task(send_initial_and_listen()),
                    asyncio.create_task(stream_gemini_to_plivo(session, None, call_state, plivo_client)),
                    asyncio.create_task(supervise_model_session(call_state, epoch)),
                ]
                try:
                    await coordinate_model_tasks(tasks)
                finally:
                    call_state['session_ready'] = False
                    cancel_silence_timer(call_state)
                    await cancel_tasks(call_state['background_tasks'])
            if call_state['terminate_session']:
                return
            raise GeminiSessionDisconnected('Gemini session task stopped unexpectedly')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            call_state['session_ready'] = False
            # A mid-turn snapshot can retain an open manual activity or replay
            # output without client turn IDs. Restore confirmed context in a
            # fresh session in that case. Idle sessions can resume directly.
            interrupted_turn = bool(call_state['responses'].turns) or call_state['user_activity_open']
            call_state['responses'].invalidate()
            from google.genai.errors import APIError
            invalid_handle = (
                isinstance(exc, APIError) and exc.code == 400
                and call_state.get('session_resumption_handle')
                and any(word in str(exc).lower() for word in ('resum', 'handle'))
            )
            if invalid_handle or interrupted_turn:
                call_state['session_resumption_handle'] = None
            if not (invalid_handle or recoverable_gemini_error(exc)):
                raise
            logger.warning('Recoverable Gemini failure (%s/%s): %s', attempt, MAX_GEMINI_RECONNECTS, exc)
            if call_state['closing_audio_phase']:
                call_state['responses'].turns.clear()
                call_state['turn_complete'] = True
                if not call_state['pending_transfer_call']:
                    call_state['pending_end_call'] = True
                playback = call_state['playback']
                if playback.dirty and not playback.marks:
                    resampler = call_state.get('output_resampler')
                    if resampler is not None:
                        playback.feed(playback.current_owner, pcm_to_ulaw(resampler.finish()))
                    playback.finish(playback.current_owner)
                # Root supervisor performs the accepted action after its mark.
                await asyncio.Event().wait()
            if attempt >= MAX_GEMINI_RECONNECTS:
                await terminate_plivo_call(plivo_client, call_state, 'Gemini recovery attempts exhausted')
                return


def create_call_state(user_name, phone_number, is_returning_from_transfer=False):
    call_state = {
        "from_number": phone_number,
        "user_name": user_name,
        "lead_saved": False,
        "end_call_summary": "",
        "is_returning_from_transfer": is_returning_from_transfer,
        "playing_disclaimer": True,
        "stream_id": None,
        "call_uuid" : None,
        "public_base_url": PUBLIC_BASE_URL,

        "greeting_completed": False,

        # Transcription Buffers ---
        "user_text_buffer": "",
        "ai_text_buffer": "",
        "conversation_log": [],

        # Acoustic echo canceller (8 kHz). Removes the AI's own voice echoed back
        # through a speakerphone mic BEFORE RNNoise/VAD, so echo can't self-trigger
        # a false barge-in loop. Far-end reference is fed by the Plivo sender.
        "aec": AcousticEchoCanceller(frame_size=PLIVO_ULAW_CHUNK_SIZE),

        # VAD
        "denoiser": RNNoise(sample_rate=48000),
        "ratecv_state_up": None,
        "ratecv_state_down": None,
        "is_speaking": False,

        "agc_current_gain_lin": 1.0,
        "chunk_count": 0,

        "user_activity_open": False,
        "assistant_speaking": False,

        # Barge-in diagnostics
        "barge_in_count": 0,

        # Model generation completion (separate from Plivo playback).
        "turn_complete": True,

        # silence handling
        "silence_timer_task": None,
        "silence_followup_count": 0,

        # deferred end-call control
        "pending_end_call": False,
        "ending_call_phase": False,
        "closing_audio_phase": False,
        "end_call_tool_executed": False,
        "terminate_session": False,
        "terminal_action_deadline": None,
        "terminal_action_completed": False,
        "terminal_action_in_progress": False,
        "hangup_started": False,
        "plivo_disconnected": False,
        "call_deadline": None,
        "end_call_summary": "",


        # transfer call control
        "pending_transfer_call": False,
        "transfer_call_tool_executed": False,
        "transfer_summary": "",
        # user context
        #"user_details": user_details,

        # audio buffers
        "preroll_pcm16": bytearray(),
        "gemini_input_buffer": bytearray(),


        # session resumption
        "session_resumption_handle": None,
        "go_away_received": False,
        "gemini_reconnect_count": 0,

        # token tracking
        "tokens_text_in": 0,
        "tokens_text_out": 0,
        "tokens_audio_in": 0,
        "tokens_audio_out": 0,
        "last_usage_total_token_count": None,
        "usage_total_updates": 0,
        "usage_total_non_monotonic_count": 0,
    }

    call_state.update({
        'responses': ResponseTracker(MODEL_RESPONSE_TIMEOUT_SECONDS),
        'speech_gate': SpeechGate(
            onset_ms=VAD_ONSET_MS, barge_ms=VAD_BARGE_IN_MS,
            offset_ms=VAD_SILENCE_OFFSET_MS, start_probability=VAD_THRESHOLD,
            barge_probability=VAD_THRESHOLD_WHILE_SPEAKING,
            minimum_db=VAD_MIN_RMS_DB, barge_minimum_db=VAD_BARGE_MIN_RMS_DB,
        ),
        'input_audio_queue': asyncio.Queue(maxsize=MAX_INPUT_QUEUE_FRAMES),
        'background_tasks': set(),
        'session_ready': False,
        'session_error': None,
        'session': None,
    })
    return call_state


@app.websocket('/media-stream')
async def handle_media_stream():
    logger.info('Client connected to Quart WebSocket')

    # Extract and sanitize inputs to prevent prompt injection
    user_name = websocket.args.get("user_name", "Unknown").replace("{", "").replace("}", "").strip()
    phone_number = websocket.args.get("phone_number", "Unknown").replace("{", "").replace("}", "").strip()

    transfer_context_id = websocket.args.get("transfer_context_id", "")
    transfer_summary_raw = websocket.args.get("transfer_summary", "")
    dial_status = websocket.args.get("dial_status", "")
    is_returning_from_transfer = (
        websocket.args.get("is_transfer_return") == "1"
        or bool(dial_status)
        or bool(transfer_context_id)
        or bool(transfer_summary_raw)
    )

    if transfer_context_id:
        transfer_context = await asyncio.to_thread(
            load_transfer_context, transfer_context_id
        )
        if transfer_context:
            phone_number = transfer_context.get("phone_number", phone_number)
            transfer_summary_raw = transfer_context.get("summary", transfer_summary_raw)
        else:
            logger.warning(
                f"No persisted transfer context found for call_uuid={transfer_context_id}"
            )

    call_summary = ""
    user_language = "Bengali"

    if is_returning_from_transfer and transfer_summary_raw:
        try:
            parsed_summary = json.loads(transfer_summary_raw)
            call_summary = parsed_summary.get("call_summary", "")
            user_language = parsed_summary.get("language", "Bengali")
        except json.JSONDecodeError:
            logger.warning("Could not parse transfer summary as JSON. Falling back to raw string.")
            call_summary = transfer_summary_raw

    logger.info(f'Client connected to Quart WebSocket. Caller: {phone_number}')
    plivo_ws = websocket

     # Dictionary to maintain state without using Python global variables
    call_state = create_call_state(user_name, phone_number, is_returning_from_transfer)
    persistent_tasks = {}
    client = None

    disclaimer_finished = asyncio.Event()
    disclaimer_task = None

    try:
        await wait_for_plivo_start(plivo_ws, call_state)
        call_state['playback'] = PlivoPlayback(
            plivo_ws, call_state['stream_id'], call_state['aec'], ulaw_to_pcm,
            lambda mark: playback_completed(call_state, mark),
            ack_timeout=PLAYBACK_ACK_TIMEOUT_SECONDS,
        )
        persistent_tasks = {
            'plivo_reader': asyncio.create_task(read_plivo_events(plivo_ws, call_state)),
            'plivo_playback': asyncio.create_task(call_state['playback'].run()),
            'supervisor': asyncio.create_task(supervise_call(call_state, plivo_client)),
        }
        async def disclaimer_lifecycle():
            await play_disclaimer(plivo_ws, call_state, disclaimer_finished)
            await asyncio.Event().wait()

        disclaimer_task = asyncio.create_task(disclaimer_lifecycle())
        persistent_tasks['disclaimer'] = disclaimer_task

        client = genai.Client(
            api_key=LIVE_API_KEY
        )

        current_time = get_indian_time()
        try:
            system_instruction_text = f"Current Date and Time (India IST): {current_time}\n\n" + RAW_SYSTEM_PROMPT.format(
                user_name=user_name,
                phone_number=phone_number
            )

            #logger.info(system_instruction_text)
        except KeyError as e:
            logger.error(f"⚠️ Missing a placeholder variable in the prompt file: {e}")
            # Fallback to direct replacement if format() fails due to unmatched braces
            system_instruction_text = f"Current Date and Time (India IST): {current_time}\n\n" + RAW_SYSTEM_PROMPT.replace("{user_name}", user_name).replace("{phone_number}", phone_number)

        if is_returning_from_transfer:
            logger.info(f"🔄 Re-entry after human transfer. DialStatus={dial_status}")
            user_context = f"The user called from {phone_number}."

            if dial_status == "completed":
                initial_prompt = f"""
                System Event: The user was previously speaking with you (Neha), was then transferred to a human admission counselor,
                the user and counselor are done now and the counselor has put down the call. The user is back on the line with you.

                {user_context}

                The user's preferred language is: **{user_language}**

                Here is a summary of the conversation before the transfer:
                {call_summary}

                Instructions:
                - Greet the user UNMISTAKABLY in {user_language} by saying - "Looks like you have spoken with our admission counselor. How can I help you now?" in {user_language}
                - Let them know you are back and ready to help.
                - Pick up naturally from where the conversation left off based on the summary above.
                - Do NOT re-introduce yourself as if this is a fresh call.
                - Speak in the users preffered language.
                - CRITICAL: DO NOT call the endCall tool right now. Wait patiently for the user to ask their next question or state what they need. Only call endCall if the user explicitly says goodbye or indicates they are completely finished.
                - IMPORTANT: *When the call eventually ends*, your summary_of_whole_call MUST cover
                    the ENTIRE call — include the pre-transfer conversation summary provided above AND
                    everything discussed in this current session after the transfer. Combine both into
                    one single complete summary.
                Please speak first now.
                """
            else:
                # busy / no-answer / failed / canceled
                initial_prompt = f"""
                System Event: The user was previously speaking with you (Neha) and requested to be transferred
                to a human admission counselor. However, the counselor was unavailable (DialStatus: {dial_status}).
                The user is back on the line with you.

                {user_context}

                The user's preferred language is: **{user_language}**

                Here is a summary of the conversation before the transfer:
                {call_summary}

                Instructions:
                - Apologise UNMISTAKABLY in {user_language} by saying exactly - "Sorry, looks like our admission counselor couldn't connect. Let me help you out instead." in {user_language}.
                - Offer to help them yourself or offer to try the transfer again if they wish.
                - Pick up naturally from where the conversation left off based on the summary above.
                - Do NOT re-introduce yourself as if this is a fresh call.
                - Speak in the users preffered language.
                - CRITICAL: DO NOT call the endCall tool right now. Wait patiently for the user to ask their next question. Only call endCall if the user explicitly says goodbye or indicates they are completely finished.
                - IMPORTANT: *When the call eventually ends*, your summary_of_whole_call MUST cover
                    the ENTIRE call — include the pre-transfer conversation summary provided above AND
                    everything discussed in this current session after the failed transfer attempt. Combine
                    both into one single complete summary.
                Please speak first now.
                """
        else:
            logger.info("Loading standard greeting prompt...")
            initial_prompt = f"""
            System Event: The outbound call to {user_name} has just connected.
            * **Greeting:** Start every call with "Namaskar {user_name}" in English, followed by: "I'm Neha, calling from the admissions team at Adamas University, Kolkata. Do you want to continue speaking in English, or switch to Hindi or Bengali?"
            * **STOP HERE:** After asking the language question, END YOUR TURN and stay silent. Do NOT mention courses, fees, admissions, or anything else yet. Wait for the user to state their language preference. Only AFTER the user replies do you continue — in their chosen language — with the consent question in the next step of the flow.
            """

        call_state['initial_prompt'] = initial_prompt
        persistent_tasks['gemini_session'] = asyncio.create_task(run_gemini_sessions(
            client, call_state, system_instruction_text, initial_prompt, disclaimer_finished,
        ))
        await coordinate_call_tasks(persistent_tasks, call_state)
        if not call_state['plivo_disconnected'] and not call_state['terminal_action_completed']:
            await terminate_plivo_call(plivo_client, call_state, 'call task ended unexpectedly')

    except asyncio.CancelledError:
        if not call_state['plivo_disconnected'] and not call_state['terminal_action_completed']:
            await terminate_plivo_call(plivo_client, call_state, 'call handler cancelled')
        raise
    except Exception:
        logger.error("Live call lifecycle failed")
        logger.error(traceback.format_exc())
        await terminate_plivo_call(plivo_client, call_state, "live call lifecycle failure")
    finally:
        call_state['terminate_session'] = True
        call_state['session_ready'] = False
        cancel_silence_timer(call_state)
        await cancel_tasks(persistent_tasks.values())
        await cancel_tasks(call_state['background_tasks'])
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.aio.aclose(), 5)
        call_state["playing_disclaimer"] = False
        if disclaimer_task is not None:
            await cancel_tasks([disclaimer_task])
        # Fallback lead capture: if the call ended without the model calling
        # endCall/transferCall (e.g. the user hung up first), no model summary
        # exists — persist a stub so no lead is lost.
        if not call_state.get("lead_saved"):
            save_lead(call_state, "user_hangup")
        log_call_stats(call_state)

async def stream_plivo_to_gemini(plivo_ws, session, call_state):
    epoch = call_state['responses'].epoch
    queue = call_state['input_audio_queue']
    while not call_state['terminate_session']:
        frame_epoch, received_at, pcm_8k = await queue.get()
        if frame_epoch != epoch:
            continue
        if call_state.get('session_error') is not None:
            raise call_state['session_error']
        if time.monotonic() - received_at > 0.5:
            raise GeminiSessionDisconnected('Inbound audio became stale')
        if call_state['closing_audio_phase'] or not call_state['greeting_completed']:
            continue
        pcm_48k, call_state['ratecv_state_up'] = audioop.ratecv(
            pcm_8k, 2, 1, PLIVO_SAMPLE_RATE, 48000, call_state['ratecv_state_up'])
        speech_probs = [float(np.mean(prob)) for prob, _ in call_state['denoiser'].denoise_chunk(
            np.frombuffer(pcm_48k, dtype=np.int16).reshape(1, -1))]
        pcm_16k, call_state['ratecv_state_down'] = audioop.ratecv(
            pcm_48k, 2, 1, 48000, GEMINI_INPUT_RATE, call_state['ratecv_state_down'])
        rms_db, samples = calculate_rms_db(pcm_16k)
        probability = float(np.mean(speech_probs)) if speech_probs else 0.0
        playback = call_state['playback']
        decision = call_state['speech_gate'].update(
            probability, rms_db, playback.busy,
        )
        call_state['is_speaking'] = call_state['speech_gate'].speaking
        started, ended = decision.started, decision.ended
        # Confidence/energy checks use pre-AGC audio; noise cannot qualify merely
        # because it was amplified. Don't chase background noise with more gain.
        gain_db = (np.clip(AGC_TARGET_DB - rms_db, 0, AGC_MAX_GAIN_DB)
                   if decision.voice_evidence and rms_db > AGC_NOISE_GATE_DB else 0)
        call_state['agc_current_gain_lin'] = (
            AGC_SMOOTHING_ALPHA * 10 ** (gain_db / 20)
            + (1 - AGC_SMOOTHING_ALPHA) * call_state['agc_current_gain_lin'])
        clean_pcm = (soft_limit(samples * call_state['agc_current_gain_lin']) * 32767).astype('<i2').tobytes()
        call_state['chunk_count'] += 1
        if started or ended or call_state['chunk_count'] % 250 == 0:
            logger.info(
                'VAD: call=%s event=%s probability=%.3f rms_db=%.1f noise_db=%.1f '
                'required_db=%.1f barge=%s rejected=%s',
                call_state.get('call_uuid'), 'start' if started else 'end' if ended else 'sample',
                probability, rms_db, decision.noise_db, decision.required_db,
                decision.barge_in, call_state['speech_gate'].rejected_candidates,
            )
        if not call_state['user_activity_open']:
            call_state['preroll_pcm16'].extend(clean_pcm)
            del call_state['preroll_pcm16'][:-PREROLL_MAX_BYTES_PCM16]
        if started:
            cancel_silence_timer(call_state)
            call_state['silence_followup_count'] = 0
            call_state['responses'].invalidate()
            call_state['user_activity_open'] = True
            call_state['turn_complete'] = False
            call_state['user_text_buffer'] = ''
            if call_state['playback'].busy:
                call_state['barge_in_count'] += 1
                await call_state['playback'].clear(reason='confirmed-user-speech')
                call_state['assistant_speaking'] = False
            emit_call_event(call_state.get('call_uuid'), 'user_speaking')
            await asyncio.wait_for(session.send_realtime_input(activity_start=types.ActivityStart()), 5)
            call_state['gemini_input_buffer'].extend(call_state['preroll_pcm16'])
            call_state['preroll_pcm16'].clear()
        elif call_state['user_activity_open']:
            call_state['gemini_input_buffer'].extend(clean_pcm)
        if call_state['user_activity_open']:
            while len(call_state['gemini_input_buffer']) >= GEMINI_PCM_CHUNK_SIZE:
                chunk = bytes(call_state['gemini_input_buffer'][:GEMINI_PCM_CHUNK_SIZE])
                del call_state['gemini_input_buffer'][:GEMINI_PCM_CHUNK_SIZE]
                await asyncio.wait_for(session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type='audio/pcm;rate=16000')), 5)
        if ended and call_state['user_activity_open']:
            if call_state['gemini_input_buffer']:
                await asyncio.wait_for(session.send_realtime_input(audio=types.Blob(
                    data=bytes(call_state['gemini_input_buffer']), mime_type='audio/pcm;rate=16000')), 5)
                call_state['gemini_input_buffer'].clear()
            # Set ownership before the send can yield to a fast server response.
            turn = call_state['responses'].begin('user')
            call_state['user_activity_open'] = False
            await asyncio.wait_for(session.send_realtime_input(activity_end=types.ActivityEnd()), 5)
            logger.info('User activity ended: call=%s turn=%s at=%.6f',
                        call_state.get('call_uuid'), turn.key, time.monotonic())
            emit_call_event(call_state.get('call_uuid'), 'user_silent')


async def stream_gemini_to_plivo(session, plivo_ws, call_state, plivo_client):
    epoch = call_state['responses'].epoch
    resampler = OutputResampler()
    call_state['output_resampler'] = resampler
    resampler_owner = None
    while not call_state['terminate_session']:
        received = False
        async for response in session.receive():
            received = True
            if epoch != call_state['responses'].epoch or call_state['terminate_session']:
                return
            update = getattr(response, 'session_resumption_update', None)
            if update and getattr(update, 'resumable', False) and getattr(update, 'new_handle', None):
                call_state['session_resumption_handle'] = update.new_handle
            go_away = getattr(response, 'go_away', None)
            if go_away is not None:
                remaining = getattr(go_away, 'time_left', None)
                try:
                    seconds = (remaining.total_seconds() if hasattr(remaining, 'total_seconds')
                               else float(str(remaining).removesuffix('s')))
                except (TypeError, ValueError):
                    seconds = 1.0
                call_state['go_away_received'] = True
                call_state['go_away_deadline'] = time.monotonic() + max(0, seconds - 1)
                emit_call_event(call_state.get('call_uuid'), 'gemini_goaway')
            tracker = call_state['responses']
            turn = tracker.current(epoch)
            content = getattr(response, 'server_content', None)
            if content is not None:
                transcript = getattr(content, 'input_transcription', None)
                if transcript and getattr(transcript, 'text', None):
                    call_state['user_text_buffer'] += transcript.text
                if getattr(content, 'interrupted', False):
                    if turn is not None:
                        turn.stale = True
                        turn.deadline = None
                        if tracker.active is turn:
                            tracker.active = None
                        if call_state['playback'].current_owner == turn.key:
                            await call_state['playback'].clear(reason='model-interruption')
                    # Keep the stale turn until its turn_complete boundary. A
                    # later interruption must never clear a newer turn's timer.
                    resampler.reset()
            valid = turn is not None and not turn.stale and not call_state['user_activity_open']
            tool_call = getattr(response, 'tool_call', None)
            if tool_call:
                replies = []
                for call in tool_call.function_calls:
                    if not valid:
                        result = {'status': 'cancelled', 'message': 'This turn was interrupted.'}
                    elif call.name not in ('endCall', 'transferCall'):
                        result = {'error': 'Unknown tool'}
                    else:
                        turn.has_tool = True
                        turn.deadline = time.monotonic() + MODEL_RESPONSE_TIMEOUT_SECONDS
                        args = dict(call.args or {})
                        call_state['closing_audio_phase'] = True
                        call_state['terminal_action_deadline'] = time.monotonic() + TERMINAL_ACTION_TIMEOUT_SECONDS
                        cancel_silence_timer(call_state)
                        if call.name == 'endCall':
                            call_state['end_call_summary'] = args.get('summary_of_whole_call', '')
                            call_state['pending_end_call'] = True
                        else:
                            call_state['transfer_summary'] = json.dumps({
                                'call_summary': args.get('call_summary', ''),
                                'language': args.get('language', ''),
                            })
                            call_state['pending_transfer_call'] = True
                        emit_call_event(call_state.get('call_uuid'), 'tool_called', {'tool_name': call.name})
                        result = {'status': 'accepted', 'message': 'Action will execute after confirmed playback.'}
                    replies.append(types.FunctionResponse(id=call.id, name=call.name, response=result))
                await asyncio.wait_for(session.send_tool_response(function_responses=replies), 5)
            if content is not None:
                output_text = getattr(content, 'output_transcription', None)
                if valid and output_text and getattr(output_text, 'text', None):
                    call_state['ai_text_buffer'] += output_text.text
                model_turn = getattr(content, 'model_turn', None)
                if valid and model_turn:
                    for part in model_turn.parts or []:
                        inline = getattr(part, 'inline_data', None)
                        if not inline or not inline.data:
                            continue
                        if resampler_owner != turn.key:
                            resampler.reset()
                            resampler_owner = turn.key
                        if not turn.has_audio:
                            logger.info('First Gemini audio: call=%s turn=%s at=%.6f',
                                        call_state.get('call_uuid'), turn.key, time.monotonic())
                            emit_call_event(call_state.get('call_uuid'), 'ai_speaking')
                        turn.has_audio = True
                        # After first audio this deadline guards stalled generation.
                        turn.deadline = time.monotonic() + MODEL_RESPONSE_TIMEOUT_SECONDS
                        audio = pcm_to_ulaw(resampler.process(inline.data))
                        call_state['playback'].feed(turn.key, audio)
                        call_state['assistant_speaking'] = True
                        cancel_silence_timer(call_state)
                if getattr(content, 'turn_complete', False):
                    status = getattr(content, 'interaction_status', None)
                    status = str(getattr(status, 'value', status) or '').upper()
                    if status == 'IN_PROGRESS' and turn is not None and not turn.stale:
                        turn.deadline = time.monotonic() + MODEL_RESPONSE_TIMEOUT_SECONDS
                        logger.info('Gemini background reasoning continues: call=%s turn=%s',
                                    call_state.get('call_uuid'), turn.key)
                        # More audio can follow this intermediate turn_complete.
                        # Retain response ownership, resampler history, and timeout.
                    else:
                        completed = tracker.complete(epoch)
                        if completed is not None and not completed.stale:
                            call_state['turn_complete'] = tracker.active is None and not call_state['user_activity_open']
                            for field, role, event in [('user_text_buffer', 'user', 'user_transcript'),
                                                       ('ai_text_buffer', 'agent', 'ai_transcript')]:
                                text = call_state[field].strip()
                                if text:
                                    logger.info('Transcript: call=%s turn=%s role=%s text=%s',
                                                call_state.get('call_uuid'), completed.key, role, text)
                                    call_state['conversation_log'].append({'role': role, 'text': text})
                                    emit_call_event(call_state.get('call_uuid'), event, {'text': text})
                                    call_state[field] = ''
                            if completed.has_audio:
                                tail = pcm_to_ulaw(resampler.finish())
                                call_state['playback'].feed(completed.key, tail)
                                tag = 'greeting' if not call_state['greeting_completed'] else 'response'
                                call_state['playback'].finish(completed.key, tag)
                            elif not completed.has_tool:
                                await recover_empty_response(session, call_state, completed)
                        else:
                            call_state['ai_text_buffer'] = ''
                        resampler.reset()
            usage = getattr(response, 'usage_metadata', None)
            if usage:
                total = getattr(usage, 'total_token_count', None)
                previous = call_state['last_usage_total_token_count']
                if total is not None:
                    if previous is not None and total < previous:
                        call_state['usage_total_non_monotonic_count'] += 1
                    call_state['last_usage_total_token_count'] = total
                call_state['usage_total_updates'] += 1
                for attr, direction in [('prompt_tokens_details', 'in'), ('response_tokens_details', 'out')]:
                    for detail in getattr(usage, attr, None) or []:
                        modality = str(detail.modality).upper()
                        kind = 'audio' if 'AUDIO' in modality else 'text'
                        call_state[f'tokens_{kind}_{direction}'] += detail.token_count or 0
        if not received:
            raise GeminiSessionDisconnected('Gemini receive ended without a turn')


async def execute_pending_terminal_action(
    plivo_ws,
    call_state,
    plivo_client,
    out_buffer,
):
    pending_end = call_state.get("pending_end_call")
    pending_transfer = call_state.get("pending_transfer_call")
    if not pending_end and not pending_transfer:
        return False
    if call_state.get("terminal_action_in_progress") or call_state.get("user_activity_open"):
        return False

    if call_state['playback'].busy:
        return False
    deadline = call_state.get('terminal_action_deadline')
    expired = deadline is not None and time.monotonic() >= deadline
    if not call_state['turn_complete'] and not expired:
        return False
    # Stop future generation before executing a terminal side effect.
    call_state['responses'].invalidate()
    call_state['terminal_action_in_progress'] = True
    call_state['ending_call_phase'] = True

    if pending_end:
        call_state["end_call_tool_executed"] = True
        call_state["pending_end_call"] = False
        await asyncio.to_thread(save_lead, call_state, "completed")
        await terminate_plivo_call(
            plivo_client,
            call_state,
            "endCall tool completed and playback drained",
        )
        return True

    call_state["transfer_call_tool_executed"] = True
    call_uuid = call_state.get("call_uuid")
    public_base_url = call_state.get("public_base_url")
    if not call_uuid or not public_base_url:
        await terminate_plivo_call(
            plivo_client,
            call_state,
            "transfer requested without call UUID or public URL",
        )
        return True

    try:
        await asyncio.to_thread(
            save_transfer_context,
            call_uuid,
            call_state.get("transfer_summary", ""),
            call_state["from_number"],
        )
        logger.info(f"Transfer context persisted for call_uuid={call_uuid}")
        emit_call_event(call_uuid, "transfer_started")

        transfer_url = (
            f"{public_base_url}/transfer.xml?call_uuid="
            f"{urllib.parse.quote(call_uuid)}"
        )
        await asyncio.to_thread(
            plivo_client.calls.transfer,
            call_uuid=call_uuid,
            legs="aleg",
            aleg_url=transfer_url,
            aleg_method="GET",
        )
        logger.info(f"Plivo call transferred via API for call_uuid={call_uuid}")
        try:
            transfer_summary = json.loads(call_state.get("transfer_summary", "") or "{}").get("call_summary", "")
        except (json.JSONDecodeError, AttributeError):
            transfer_summary = call_state.get("transfer_summary", "")
        await asyncio.to_thread(save_lead, call_state, "transferred", transfer_summary)
    except Exception as e:
        logger.error(f"Failed to transfer call: {e}")
        await terminate_plivo_call(
            plivo_client,
            call_state,
            "transfer operation failed",
        )
        return True

    call_state["pending_transfer_call"] = False
    call_state["terminal_action_completed"] = True
    call_state["terminate_session"] = True
    await asyncio.sleep(POST_TRANSFER_DELAY_SECONDS)
    with contextlib.suppress(Exception):
        if plivo_ws is not None:
            await plivo_ws.close(1000)
    return True


@app.route("/trigger-call", methods=["POST"])
async def trigger_call():
    api_key = request.headers.get("X-API-Key")
    if not api_key or api_key != API_AUTH_TOKEN:
        logger.warning(f"Unauthorized access attempt to /trigger-call from {request.remote_addr}")
        return {"status": "error", "message": "Unauthorized"}, 401

    data = await request.get_json()

    target_user_name = data.get("user_name", "Unknown")
    target_phone_number = data.get("phone_number")

    if not target_phone_number:
        return {"status": "error", "message": "Phone number is required"}, 400

    encoded_name = urllib.parse.quote(target_user_name)
    encoded_phone = urllib.parse.quote(target_phone_number)

    answer_url = f"{PUBLIC_BASE_URL}/outbound-webhook?user_name={encoded_name}&phone_number={encoded_phone}"

    try:
        logger.info(f"Initiating outbound call to {target_phone_number} via Dashboard...")
        call_made = await asyncio.to_thread(
            plivo_client.calls.create,
            from_=PLIVO_PHONE_NUMBER,
            to_="+91" + target_phone_number,
            answer_url=answer_url,
            answer_method='GET',
        )
        logger.info(f"✅ Outbound call successfully queued: {call_made}")
        call_id = str(getattr(call_made, 'request_uuid', call_made))
        emit_call_event(call_id, "call_queued", {"name": target_user_name, "phone": target_phone_number})
        return {"status": "success", "message": "Call queued", "call_uuid": call_id}, 200
    except Exception as e:
        logger.error(f"❌ Failed to initiate outbound call: {e}")
        emit_call_event("", "call_failed", {"name": target_user_name, "phone": target_phone_number, "error": str(e)})
        return {"status": "error", "message": str(e)}, 500

if __name__ == "__main__":
    logger.info(f'Starting the Quart Server on Port {PORT} with Hypercorn (Waiting for Dashboard trigger...)')
    import hypercorn.asyncio
    from hypercorn.config import Config as HypercornConfig

    hconfig = HypercornConfig()
    hconfig.bind = [f"0.0.0.0:{PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, hconfig))
