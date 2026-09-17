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

VAD_THRESHOLD = 0.75

VAD_THRESHOLD_WHILE_SPEAKING = 0.82
VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING = 4   # ~80 ms of sustained speech to barge in

PREROLL_MAX_BYTES_PCM16 = 6400       # ~200 ms @ 16kHz PCM16 (Replacing PREROLL_MAX_BYTES_PCM8)

# DSP Constants
AGC_TARGET_DB = -12.0
AGC_MAX_GAIN_DB = 30.0               # Tuned down to prevent telephony artifact amplification
AGC_NOISE_GATE_DB = -50.0
AGC_SMOOTHING_ALPHA = 0.08         # Slow alpha to prevent volume pumping

VAD_SPEECH_ONSET_FRAMES = 3
VAD_SILENCE_OFFSET_FRAMES = 10   # ~200 ms trailing silence before end-of-turn (was 15 = 300 ms)
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
    try:
        await asyncio.sleep(delay)
        if call_state.get("terminate_session") or call_state.get("user_activity_open"):
            return
        if call_state["silence_followup_count"] >= MAX_SILENCE_FOLLOWUPS:
            logger.info("Maximum silence follow-ups reached. Sending farewell prompt before ending call.")
            call_state["closing_audio_phase"] = True
            call_state["awaiting_model"] = True
            call_state["model_response_deadline"] = time.monotonic() + MODEL_RESPONSE_TIMEOUT_SECONDS
            call_state["terminal_action_deadline"] = time.monotonic() + TERMINAL_ACTION_TIMEOUT_SECONDS
            await session.send_realtime_input(text=SILENCE_FAREWELL_PROMPT)
            return
        call_state["silence_followup_count"] += 1
        call_state["awaiting_model"] = True
        call_state["model_response_deadline"] = time.monotonic() + MODEL_RESPONSE_TIMEOUT_SECONDS
        logger.info(f"Prompting AI to re-engage ({call_state['silence_followup_count']}/{MAX_SILENCE_FOLLOWUPS}).")
        await session.send_realtime_input(text=SILENCE_FOLLOWUP_PROMPT)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Silence watchdog failed: {e}")
        call_state["pending_end_call"] = True
        call_state["closing_audio_phase"] = True
        call_state["terminal_action_deadline"] = time.monotonic() + 1.0

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
                payload = await q.get()
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            call_event_subscribers.discard(q)

    return Response(
        event_generator(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )

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
#         <Record action="{PUBLIC_BASE_URL}/recording-callback" redirect="false" recordSession="true" maxLength="3600" />
#         <Play>{AUDIO_URL}</Play>
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
        <Record action="{PUBLIC_BASE_URL}/recording-callback" redirect="false" recordSession="true" maxLength="3600" />
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
            await asyncio.to_thread(
                plivo_client.calls.delete,
                call_uuid=call_uuid,
            )
            logger.info(f"Plivo call explicitly terminated for call_uuid={call_uuid}")
        else:
            logger.warning("Cannot explicitly terminate Plivo call because call_uuid is unavailable")
    except Exception as e:
        logger.error(f"Failed to terminate Plivo call: {e}")
    finally:
        call_state["terminal_action_completed"] = True
        call_state["terminate_session"] = True


async def supervise_call(call_state, plivo_client):
    while not call_state.get("terminate_session"):
        now = time.monotonic()

        if now >= call_state["call_deadline"]:
            await terminate_plivo_call(plivo_client, call_state, "maximum call duration reached")
            return

        response_deadline = call_state.get("model_response_deadline")
        if (
            call_state.get("awaiting_model")
            and response_deadline is not None
            and now >= response_deadline
        ):
            await terminate_plivo_call(plivo_client, call_state, "Gemini response timeout")
            return
        await asyncio.sleep(SUPERVISOR_POLL_INTERVAL)


@contextlib.asynccontextmanager
async def connect_live_with_timeout(client, model, config):
    live_context = client.aio.live.connect(model=model, config=config)
    session = await asyncio.wait_for(
        live_context.__aenter__(),
        timeout=GEMINI_CONNECT_TIMEOUT_SECONDS,
    )

    try:
        yield session
    except BaseException as exc:
        try:
            suppress_exception = await live_context.__aexit__(
                type(exc), exc, exc.__traceback__
            )
            if not suppress_exception:
                raise
        except Exception as cleanup_exc:
            from google.genai import errors as genai_errors
            if isinstance(cleanup_exc, genai_errors.APIError) and "1000" in str(cleanup_exc):
                pass
            else:
                raise
    else:
        try:
            await live_context.__aexit__(None, None, None)
        except Exception as cleanup_exc:
            from google.genai import errors as genai_errors
            if isinstance(cleanup_exc, genai_errors.APIError) and "1000" in str(cleanup_exc):
                pass
            else:
                raise


async def coordinate_call_tasks(task_map, call_state):
    done, pending = await asyncio.wait(
        task_map.values(),
        return_when=asyncio.FIRST_COMPLETED,
    )
    completed_names = {
        name for name, task in task_map.items() if task in done
    }
    call_state["terminate_session"] = True

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        if task.cancelled():
            continue
        exception = task.exception()
        if exception is not None:
            raise exception

    return completed_names


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
    if not DISCLAIMER_ULAW_CHUNKS or call_state.get("is_returning_from_transfer"):
        logger.warning("No disclaimer chunks found. Skipping directly to AI greeting.")
        call_state["playing_disclaimer"] = False
        disclaimer_finished_event.set()
        return
        
    logger.info("📢 Playing legal disclaimer to user...")
    try:
        start_time = time.perf_counter()
        for i, chunk in enumerate(DISCLAIMER_ULAW_CHUNKS):
            if call_state.get("terminate_session", False):
                break
            audio_delta = {
                "event": "playAudio",
                "media": {
                    "contentType": "audio/x-mulaw",
                    "sampleRate": 8000,
                    "payload": chunk
                }
            }
            await plivo_ws.send(json.dumps(audio_delta))
            expected_time = start_time + ((i + 1) * 0.02)
            sleep_time = expected_time - time.perf_counter()
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            
        logger.info("✅ Disclaimer finished. Unlocking Gemini prompt.")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Disclaimer task error: {e}")
    finally:
        call_state["playing_disclaimer"] = False
        disclaimer_finished_event.set() # 🟢 Flip the traffic light to GREEN!

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
        "ratecv_state_out": None,
        "rnnoise_speech_count": 0,
        "rnnoise_silence_frames": 0,
        "is_speaking": False,
        
        "agc_current_gain_lin": 1.0,
        "chunk_count": 0,
        
        "user_activity_open": False,
        "assistant_speaking": False,
        "awaiting_model": False,
        "model_response_deadline": None,
        "interrupting": False,
        
        # AI speech timing trackers
        "ai_playback_start_time": None,
        "current_utterance_bytes": 0,
        "turn_complete": True,
        
        # silence handling
        "silence_timer_task": None,
        "silence_followup_count": 0,
        
        # deferred end-call control
        "pending_end_call": False,
        "ending_call_phase": False,
        "closing_audio_phase": False,
        "closing_audio_started": False,
        "end_call_tool_executed": False,
        "terminate_session": False,
        "terminal_action_deadline": None,
        "terminal_action_completed": False,
        "terminal_action_in_progress": False,
        "hangup_started": False,
        "plivo_disconnected": False,
        "call_deadline": None,
        "end_call_summary": "", 
        
        # unified finally analytics control
        "analytics_saved": False,
        "skip_finally_analytics": False,
        
        # transfer call control
        "pending_transfer_call": False,
        "transfer_call_tool_executed": False,
        "transfer_summary": "", 
        # user context
        #"user_details": user_details,
        
        # audio buffers
        "preroll_pcm16": bytearray(),
        "gemini_input_buffer": bytearray(),
        "plivo_output_queue": asyncio.Queue(),
        
        # helpers
        "closing": False,
        "tool_call_in_progress": False,
        
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
    
    disclaimer_finished = asyncio.Event()
    disclaimer_task = None

    try:
        await wait_for_plivo_start(plivo_ws, call_state)
        disclaimer_task = asyncio.create_task(
            play_disclaimer(plivo_ws, call_state, disclaimer_finished)
        )

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
        
        # === SESSION MANAGEMENT: Reconnection Loop ===
        for _attempt in range(MAX_GEMINI_RECONNECTS + 1):
            is_reconnect = call_state["gemini_reconnect_count"] > 0

            if is_reconnect:
                # If the caller already hung up, don't spin a fresh Gemini session
                # against a dead Plivo socket — just stop.
                if call_state.get("plivo_disconnected"):
                    logger.info("Plivo already disconnected during disconnect. Not reconnecting Gemini.")
                    break

                # If a terminal action was in progress, don't reconnect — just terminate
                if call_state.get("closing_audio_phase") or call_state.get("pending_end_call") or call_state.get("pending_transfer_call"):
                    logger.info("Terminal action was in progress during disconnect. Terminating call.")
                    await terminate_plivo_call(plivo_client, call_state, "disconnect during terminal action")
                    break

                logger.info(
                    f"🔄 Gemini reconnection attempt {call_state['gemini_reconnect_count']}/{MAX_GEMINI_RECONNECTS} "
                    f"(handle={'present' if call_state.get('session_resumption_handle') else 'none'})"
                )
                emit_call_event(call_state.get("call_uuid"), "gemini_reconnecting", {
                    "attempt": call_state["gemini_reconnect_count"],
                })
                await asyncio.sleep(GEMINI_RECONNECT_DELAY_SECONDS)

            # Build config with the current resumption handle (if any)
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
                    thinking_level="low",
                ),
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name="Kore"
                        )
                    )
                ),
                realtime_input_config=types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(
                        disabled=True,
                    ),
                    activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                ),
                context_window_compression=(
                    types.ContextWindowCompressionConfig(
                        sliding_window=types.SlidingWindow(),
                    )
                )
            )

            # Reset transient state for the new connection
            call_state["go_away_received"] = False
            call_state["terminate_session"] = False
            call_state["user_activity_open"] = False
            call_state["is_speaking"] = False
            call_state["rnnoise_speech_count"] = 0
            call_state["rnnoise_silence_frames"] = 0
            call_state["awaiting_model"] = False
            call_state["model_response_deadline"] = None
            call_state["interrupting"] = False
            call_state["tool_call_in_progress"] = False
            call_state["gemini_input_buffer"] = bytearray()
            call_state["preroll_pcm16"] = bytearray()
            call_state["ratecv_state_up"] = None
            call_state["ratecv_state_down"] = None
            call_state["ratecv_state_out"] = None
            # Clear the AEC far-end backlog on reconnect: the disconnect gap
            # desyncs far/near timing. Keep the learned filter weights — the
            # acoustic echo path is unchanged across a Gemini reconnect.
            call_state["aec"].reset_far_end()

            try:
                async with connect_live_with_timeout(client, GEMINI_MODEL, config) as session:
                    if is_reconnect:
                        logger.info("✅ Gemini session resumed successfully")
                        emit_call_event(call_state.get("call_uuid"), "gemini_reconnected")
                    else:
                        logger.info('Connected to Google GenAI Live API')

                    if not is_reconnect:
                        logger.info("Waiting for disclaimer audio to finish playing...")
                        await disclaimer_finished.wait()

                        logger.info("Disclaimer done. Sending initial context to trigger AI greeting...")
                        await session.send_client_content(
                            turns=types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=initial_prompt)],
                            ),
                            turn_complete=True,
                        )
                        call_state["awaiting_model"] = True
                        call_state["model_response_deadline"] = (
                            time.monotonic() + MODEL_RESPONSE_TIMEOUT_SECONDS
                        )

                    task_map = {
                        "plivo_input": asyncio.create_task(
                            stream_plivo_to_gemini(plivo_ws, session, call_state)
                        ),
                        "gemini_output": asyncio.create_task(
                            stream_gemini_to_plivo(session, plivo_ws, call_state, plivo_client)
                        ),
                        "plivo_output": asyncio.create_task(
                            send_plivo_audio(plivo_ws, call_state, session, plivo_client)
                        ),
                        "supervisor": asyncio.create_task(
                            supervise_call(call_state, plivo_client)
                        ),
                    }

                    try:
                        await coordinate_call_tasks(task_map, call_state)
                        if (
                            not call_state["plivo_disconnected"]
                            and not call_state["terminal_action_completed"]
                        ):
                            await terminate_plivo_call(
                                plivo_client,
                                call_state,
                                "call task ended unexpectedly",
                            )
                    finally:
                        if call_state.get("silence_timer_task") and not call_state["silence_timer_task"].done():
                            call_state["silence_timer_task"].cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await call_state["silence_timer_task"]

                # If we got here cleanly (no exception), the call ended normally
                break

            except GeminiSessionDisconnected:
                call_state["gemini_reconnect_count"] += 1
                if call_state["gemini_reconnect_count"] > MAX_GEMINI_RECONNECTS:
                    logger.error(f"❌ Exhausted all {MAX_GEMINI_RECONNECTS} Gemini reconnection attempts")
                    await terminate_plivo_call(
                        plivo_client, call_state,
                        "exhausted Gemini reconnection attempts",
                    )
                    break
                logger.info("🔄 Gemini session disconnected, will attempt reconnection...")
                continue  # Go to top of for loop for next attempt

    except asyncio.CancelledError:
        logger.info('Client disconnected')
        raise
    except Exception:
        logger.error("Live call lifecycle failed")
        logger.error(traceback.format_exc())
        await terminate_plivo_call(plivo_client, call_state, "live call lifecycle failure")
    finally:
        call_state["playing_disclaimer"] = False
        if disclaimer_task is not None and not disclaimer_task.done():
            disclaimer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await disclaimer_task
        # Fallback lead capture: if the call ended without the model calling
        # endCall/transferCall (e.g. the user hung up first), no model summary
        # exists — persist a stub so no lead is lost.
        if not call_state.get("lead_saved"):
            save_lead(call_state, "user_hangup")
        log_call_stats(call_state)

async def stream_plivo_to_gemini(plivo_ws, session, call_state):
    logger.info('Ready to stream audio from Plivo to Gemini')

    try:
        while True:
            if call_state["terminate_session"]:
                logger.info("🎬 Terminating Plivo -> Gemini loop")
                break
            message = await plivo_ws.receive()
            if message is None:
                call_state["plivo_disconnected"] = True
                call_state["terminate_session"] = True
                break

            data = json.loads(message)

            if data['event'] == 'start':
                apply_plivo_start(call_state, data.get('start', {}))

            elif data['event'] == 'media':
                payload = data.get('media', {}).get('payload')
                if not payload:
                    continue
                
                # Ignore user audio if we are ending the call OR if the disclaimer is actively playing
                if call_state.get("closing_audio_phase") or call_state.get("playing_disclaimer"):
                    continue

                mulaw_chunk = base64.b64decode(payload)
                pcm_8k = ulaw_to_pcm(mulaw_chunk)

                # Acoustic echo cancellation FIRST — strip the AI's echoed voice
                # (speakerphone) before any noise suppression / VAD sees it. When
                # the AI isn't speaking the far-end is silent and this is a
                # near-transparent passthrough.
                pcm_8k = call_state["aec"].process(pcm_8k)
                if not pcm_8k:
                    continue

                # ==========================================
                # 1. DSP Pipeline: Preprocessing & Denoising
                # ==========================================
                noise_start_time = time.perf_counter()
                
                # Upsample 8k → 48k
                pcm_48k, call_state["ratecv_state_up"] = audioop.ratecv(
                    pcm_8k, 2, 1, PLIVO_SAMPLE_RATE, 48000, call_state["ratecv_state_up"]
                )
                samples_48k = np.frombuffer(pcm_48k, dtype=np.int16)
            
                denoised_samples = []
                speech_probs = []
                chunk_48k = samples_48k.reshape(1, -1)
                
                for speech_prob, clean_frame in call_state["denoiser"].denoise_chunk(chunk_48k):
                    denoised_samples.append(np.array(clean_frame, dtype=np.int16).flatten())
                    speech_probs.append(speech_prob)
                
                clean_pcm_48k = np.concatenate(denoised_samples).tobytes() if denoised_samples else b""
                
                # Downsample 48k → 16k
                clean_pcm_16k, call_state["ratecv_state_down"] = audioop.ratecv(
                    clean_pcm_48k, 2, 1, 48000, GEMINI_INPUT_RATE, call_state["ratecv_state_down"]
                )
                
                rms_db, float_samples = calculate_rms_db(clean_pcm_16k)

                if rms_db > AGC_NOISE_GATE_DB:
                    target_gain_db = AGC_TARGET_DB - rms_db
                    target_gain_db = np.clip(target_gain_db, 0, 15.0) 
                else:
                    target_gain_db = 0.0

                target_gain_lin = 10 ** (target_gain_db / 20.0)

                call_state["agc_current_gain_lin"] = (AGC_SMOOTHING_ALPHA * target_gain_lin) + ((1.0 - AGC_SMOOTHING_ALPHA) * call_state["agc_current_gain_lin"])

                processed_samples = float_samples * call_state["agc_current_gain_lin"]
                
                limit_threshold = 0.9
                processed_samples = np.where(
                    np.abs(processed_samples) < limit_threshold,
                    processed_samples,
                    limit_threshold * np.sign(processed_samples) + 
                    (1.0 - limit_threshold) * np.tanh((np.abs(processed_samples) - limit_threshold) / (1.0 - limit_threshold))
                )

                # Final clean 16kHz PCM
                processed_samples_int16 = (processed_samples * 32767.0).astype(np.int16)
                clean_pcm_16k = processed_samples_int16.tobytes()
                
                if call_state["chunk_count"] % LOG_EVERY_N_CHUNKS == 0:
                    current_gain_db_log = 20 * np.log10(call_state["agc_current_gain_lin"] + 1e-12)
                    logger.info(f"🔊 [AGC] Inbound RMS: {rms_db:.1f} dB | Gain: +{current_gain_db_log:.1f} dB")

                noise_end_time = time.perf_counter()
                
                avg_prob = float(np.mean(speech_probs)) if speech_probs else 0.0
                speech_started = False
                speech_ended = False

                # Half-duplex echo gate: raise the bar for detecting a NEW speech
                # onset while the assistant is talking, so the AI's own echoed voice
                # (no AEC in this pipeline) cannot self-trigger a false barge-in loop.
                # Once the user is already established as speaking, keep the normal
                # onset/offset thresholds so genuine turns aren't cut short.
                if call_state["assistant_speaking"] and not call_state["is_speaking"]:
                    active_threshold = VAD_THRESHOLD_WHILE_SPEAKING
                    active_onset_frames = VAD_SPEECH_ONSET_FRAMES_WHILE_SPEAKING
                else:
                    active_threshold = VAD_THRESHOLD
                    active_onset_frames = VAD_SPEECH_ONSET_FRAMES

                if avg_prob > active_threshold:  # Threshold for confident speech
                    call_state["rnnoise_speech_count"] += 1
                    call_state["rnnoise_silence_frames"] = 0
                    if call_state["rnnoise_speech_count"] >= active_onset_frames and not call_state["is_speaking"]:
                        call_state["is_speaking"] = True
                        speech_started = True
                else:
                    call_state["rnnoise_speech_count"] = 0
                    if call_state["is_speaking"]:
                        call_state["rnnoise_silence_frames"] += 1
                        if call_state["rnnoise_silence_frames"] >= VAD_SILENCE_OFFSET_FRAMES:
                            call_state["is_speaking"] = False
                            speech_ended = True
                            
                call_state["chunk_count"] += 1
                if call_state["chunk_count"] % LOG_EVERY_N_CHUNKS == 0:
                    latency_ms = (noise_end_time - noise_start_time) * 1000
                    logger.info(f"🎧 [RNNoise] Latency: {latency_ms:.2f} ms | VAD Prob: {avg_prob:.2f}")
                    
                if not call_state.get("greeting_completed", False):
                    continue
                
                # Maintain ~200ms preroll using the CLEAN 16kHz audio
                if not call_state["user_activity_open"]:
                    call_state["preroll_pcm16"].extend(clean_pcm_16k)
                    if len(call_state["preroll_pcm16"]) > PREROLL_MAX_BYTES_PCM16:
                        overflow = len(call_state["preroll_pcm16"]) - PREROLL_MAX_BYTES_PCM16
                        del call_state["preroll_pcm16"][:overflow]

                chunk_already_buffered_via_preroll = False

                if speech_started:
                    logger.info("🎤 User speech detected")
                    emit_call_event(call_state.get("call_uuid"), "user_speaking")
                    call_state["silence_followup_count"] = 0
                    call_state["awaiting_model"] = False
                    call_state["model_response_deadline"] = None
                    
                    # Cancel the silence timer if active
                    if call_state.get("silence_timer_task") and not call_state["silence_timer_task"].done():
                        call_state["silence_timer_task"].cancel()
                        call_state["silence_timer_task"] = None

                    # Barge-in: if assistant is speaking, stop local queue and Plivo playback immediately
                    if call_state["assistant_speaking"]:
                        call_state["interrupting"] = True
                        call_state["assistant_speaking"] = False
                        
                        # Log the exact time the AI was cut off
                        interrupted_time = datetime.now(ist_tz)
                        logger.info(f"🎙️ [TIMING] AI Speech Interrupted at: {interrupted_time.strftime('%H:%M:%S.%f')[:-3]}")
                        
                        # Reset timing trackers
                        call_state["ai_playback_start_time"] = None
                        call_state["current_utterance_bytes"] = 0

                        # Drain queued outbound audio
                        while not call_state["plivo_output_queue"].empty():
                            with contextlib.suppress(asyncio.QueueEmpty):
                                call_state["plivo_output_queue"].get_nowait()

                        if call_state.get("stream_id"):
                            await plivo_ws.send(json.dumps({
                                "event": "clearAudio",
                                "stream_id": call_state["stream_id"]
                            }))
                            logger.info("🛑 Cleared Plivo playback buffer")

                    if not call_state["user_activity_open"]:
                        if not call_state["tool_call_in_progress"]:
                            # Set the flag BEFORE the await so the concurrent gemini
                            # task cannot also open activity in the same window
                            # (avoids a double activityStart with no activityEnd).
                            call_state["user_activity_open"] = True
                            call_state["awaiting_model"] = False
                            call_state["model_response_deadline"] = None
                            call_state["turn_complete"] = False
                            await session.send_realtime_input(
                                activity_start=types.ActivityStart()
                            )
                            logger.info("▶️ Sent activityStart to Gemini")

                            # Flush the clean preroll buffer
                            if call_state["preroll_pcm16"]:
                                call_state["gemini_input_buffer"].extend(call_state["preroll_pcm16"])
                                call_state["preroll_pcm16"].clear()
                                chunk_already_buffered_via_preroll = True
                            
                        call_state["interrupting"] = False

                # While user activity is open, continuously stream audio to Gemini
                if call_state["user_activity_open"] and not chunk_already_buffered_via_preroll:
                    call_state["gemini_input_buffer"].extend(clean_pcm_16k)

                    while len(call_state["gemini_input_buffer"]) >= GEMINI_PCM_CHUNK_SIZE:
                        audio_chunk = bytes(call_state["gemini_input_buffer"][:GEMINI_PCM_CHUNK_SIZE])
                        del call_state["gemini_input_buffer"][:GEMINI_PCM_CHUNK_SIZE]
                        if not call_state["tool_call_in_progress"]:
                            await session.send_realtime_input(
                                audio=types.Blob(data=audio_chunk, mime_type="audio/pcm;rate=16000")
                            )
                if speech_ended and call_state["user_activity_open"]:
                    logger.info(" 🔇 User speech end detected")
                    emit_call_event(call_state.get("call_uuid"), "user_silent")

                    # Flush remainder before ending activity
                    if call_state["gemini_input_buffer"]:
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=bytes(call_state["gemini_input_buffer"]),
                                mime_type="audio/pcm;rate=16000"
                            )
                        )
                        call_state["gemini_input_buffer"].clear()

                    await session.send_realtime_input(
                        activity_end=types.ActivityEnd()
                    )
                    call_state["user_activity_open"] = False
                    call_state["awaiting_model"] = True
                    call_state["model_response_deadline"] = (
                        time.monotonic() + MODEL_RESPONSE_TIMEOUT_SECONDS
                    )
                    call_state["interrupting"] = False
                    logger.info("⏹️ Sent activityEnd to Gemini")

            elif data['event'] == 'stop':
                logger.info('Plivo stream stopped')
                call_state["plivo_disconnected"] = True
                call_state["terminate_session"] = True
                break

    except asyncio.CancelledError:
        logger.info("Plivo -> Gemini stream cancelled")
        raise
    except Exception as e:
        logger.error(f"Error in Plivo -> Gemini stream: {e}")
        raise

async def stream_gemini_to_plivo(session, plivo_ws, call_state,plivo_client):
    logger.info('Ready to stream audio from Gemini to Plivo')
    try:
        while True:
            async for response in session.receive():
                
                # ==========================================
                # 0. To end the call
                # ==========================================
                if call_state["terminate_session"]:
                    logger.info("Terminating Gemini -> Plivo loop")
                    break
                
                # ==========================================
                # 1. NEW: Handle Tool Calls at the Root Level
                # ==========================================
                if response.tool_call:
                    call_state["tool_call_in_progress"] = True
                    call_state["awaiting_model"] = False
                    call_state["model_response_deadline"] = None
                    function_responses_to_send = []
                    
                    try:
                    
                        for call in response.tool_call.function_calls:
                            logger.info(f"\n[⚙️ Gemini requested tool execution: {call.name}]")
                            emit_call_event(call_state.get("call_uuid"), "tool_called", {"tool_name": call.name})
                            args_dict = dict(call.args) if call.args else {}
                            logger.info(f"Arguments: {args_dict}")

                            try:
                                if call.name == "endCall":
                                    end_call_summary = args_dict.get("summary_of_whole_call", "")
                                    call_state["end_call_summary"] = end_call_summary
                                    logger.info(f"📋 End-call summary captured from Gemini: {end_call_summary}")
                                    call_state["pending_end_call"] = True
                                    call_state["closing_audio_phase"] = True
                                    call_state["terminal_action_deadline"] = (
                                        time.monotonic() + TERMINAL_ACTION_TIMEOUT_SECONDS
                                    )
                                    call_state["end_call_tool_executed"] = False
                                    call_state["terminate_session"] = False
                                    
                                    # Reset VAD state to prevent barge-in from carrying over stale context
                                    call_state["is_speaking"] = False
                                    call_state["user_activity_open"] = False
                                    
                                    logger.info("📴 Call ending requested by Gemini; MCP endCall will execute after closing audio playback finishes")

                                    function_responses_to_send.append(
                                        types.FunctionResponse(
                                            id=call.id,
                                            name=call.name,
                                            response={
                                                "status": "accepted", 
                                                "message": "Call will end after audio playback completes."
                                            }
                                        )
                                    )
                                elif call.name == "transferCall":
                                    call_summary = args_dict.get("call_summary", "")
                                    language = args_dict.get("language", "")
                                    
                                    structured_summary = {
                                        "call_summary": call_summary,
                                        "language": language
                                    }
                                    
                                    summary_json_string = json.dumps(structured_summary)
                                    call_state["transfer_summary"] = summary_json_string
                                    logger.info(f"📋 Summary captured from Gemini: {summary_json_string}")
                                    
                                    call_state["pending_transfer_call"] = True
                                    call_state["closing_audio_phase"] = True
                                    call_state["terminal_action_deadline"] = (
                                        time.monotonic() + TERMINAL_ACTION_TIMEOUT_SECONDS
                                    )
                                    call_state["transfer_call_tool_executed"] = False
                                    call_state["terminate_session"] = False
                                    
                                    # Reset VAD state to prevent barge-in from carrying over stale context
                                    call_state["is_speaking"] = False
                                    call_state["user_activity_open"] = False
                                    logger.info("🔀 Call transfer requested by Gemini; MCP transferCall will execute after audio playback finishes")

                                    function_responses_to_send.append(
                                        types.FunctionResponse(
                                            id=call.id,
                                            name=call.name,
                                            response={
                                                "status": "accepted", 
                                                "message": "Call will be transferred after audio playback completes."
                                            }
                                        )
                                    )
                                    
                                # else:
                                #     # Execute against Adamas Tech server
                                #     mcp_result = await mcp_session.call_tool(
                                #         call.name, args_dict
                                #     )
                                #     result_text = "\n".join(
                                #         [
                                #             c.text
                                #             for c in mcp_result.content
                                #             if c.type == "text"
                                #         ]
                                #     )
                                #     logger.info("[✅ Tool executed successfully. Returning data to Gemini...]")
                                #     logger.info(f"Raw Data from MCP: {result_text}")

                                #     function_responses_to_send.append(
                                #         types.FunctionResponse(
                                #             id=call.id,
                                #             name=call.name,
                                #             response={
                                #                 "result": result_text,
                                #                 # schedule
                                #                 # "scheduling": "WHEN_IDLE"
                                #             },
                                #         )
                                #     )
                            except Exception as e:
                                logger.error(f"[❌ Error executing MCP tool {call.name}: {e}]")
                                function_responses_to_send.append(
                                    types.FunctionResponse(
                                        id=call.id,
                                        name=call.name,
                                        response={"error": str(e)},
                                    )
                                )
                            
                    finally:
                        if function_responses_to_send:
                            await session.send_tool_response(
                                function_responses=function_responses_to_send
                            )
                        call_state["tool_call_in_progress"] = False
                        
                        if call_state.get("is_speaking") and not call_state.get("user_activity_open"):
                            # Set the flag before the await to avoid a double
                            # activityStart racing the Plivo input task.
                            call_state["user_activity_open"] = True
                            call_state["awaiting_model"] = False
                            call_state["model_response_deadline"] = None
                            call_state["turn_complete"] = False
                            await session.send_realtime_input(
                                activity_start=types.ActivityStart()
                            )
                            logger.info("▶️ Sent deferred activityStart to Gemini (tool call finished)")

                            # Speech that began DURING the tool call was captured in
                            # preroll (activity was not open yet). Prepend it so the
                            # onset of the user's utterance isn't clipped, then flush
                            # anything already in the input buffer.
                            if call_state["preroll_pcm16"]:
                                call_state["gemini_input_buffer"][:0] = call_state["preroll_pcm16"]
                                call_state["preroll_pcm16"].clear()
                            if call_state["gemini_input_buffer"]:
                                await session.send_realtime_input(
                                    audio=types.Blob(
                                        mime_type="audio/pcm;rate=16000",
                                        data=bytes(call_state["gemini_input_buffer"]),
                                    )
                                )
                                call_state["gemini_input_buffer"].clear()

                # ==========================================
                # 2. Handle Audio and Interruptions
                # ==========================================
                server_content = getattr(response, 'server_content', None)
                if server_content is not None:
                    
                    # 1. Accumulate User Transcriptions silently
                    input_transcription = getattr(server_content, 'input_transcription', None)
                    if input_transcription and getattr(input_transcription, 'text', None):
                        call_state["user_text_buffer"] += input_transcription.text
                        
                    # 2. Accumulate AI Transcriptions silently
                    output_transcription = getattr(server_content, 'output_transcription', None)
                    if output_transcription and getattr(output_transcription, 'text', None):
                        call_state["ai_text_buffer"] += output_transcription.text
                    
                    if getattr(server_content, 'interrupted', False):
                        logger.info("🛑 Gemini confirmed interruption")
                        
                        if call_state["ai_text_buffer"].strip():
                            logger.info(f"🤖 [GEMINI] (Interrupted): {call_state['ai_text_buffer'].strip()}")
                            call_state["ai_text_buffer"] = ""
                            
                        call_state["assistant_speaking"] = False
                        call_state["awaiting_model"] = False
                        call_state["model_response_deadline"] = None
                        call_state["interrupting"] = False
                        if not call_state["closing_audio_phase"]:
                            call_state["pending_end_call"] = False
                            call_state["pending_transfer_call"] = False
                            call_state["ending_call_phase"] = False
                            call_state["closing_audio_started"] = False
                        call_state["turn_complete"] = True
                        
                        while not call_state["plivo_output_queue"].empty():
                            with contextlib.suppress(asyncio.QueueEmpty):
                                call_state["plivo_output_queue"].get_nowait()
                                
                        if call_state.get("stream_id"):
                            await plivo_ws.send(json.dumps({
                                "event": "clearAudio",
                                "stream_id": call_state["stream_id"]
                            }))
                    
                    if getattr(server_content, 'turn_complete', False):
                        call_state["turn_complete"] = True
                        
                        # Flush the completed AI sentence to the logs
                        if call_state["ai_text_buffer"].strip():
                            ai_transcript = call_state["ai_text_buffer"].strip()
                            logger.info(f"🤖 [GEMINI]: {ai_transcript}")
                            call_state["conversation_log"].append({
                                "role": "agent",
                                "text": ai_transcript
                            })
                            emit_call_event(call_state.get("call_uuid"), "ai_transcript", {"text": ai_transcript})
                            call_state["ai_text_buffer"] = ""
                    
                    model_turn = getattr(server_content, 'model_turn', None)
                    #logger.info("⚡ Gemini Audio Chunk Received!")
                    if model_turn is not None:
                        call_state["awaiting_model"] = False
                        call_state["model_response_deadline"] = None
                        
                        # Flush the completed user sentence to the logs now.
                        if call_state["user_text_buffer"].strip() and not call_state["assistant_speaking"]:
                            user_transcript = call_state["user_text_buffer"].strip()
                            logger.info(f"🗣️ [USER]: {user_transcript}")
                            call_state["conversation_log"].append({
                                "role": "user",
                                "text": user_transcript
                            })
                            emit_call_event(call_state.get("call_uuid"), "user_transcript", {"text": user_transcript})
                            call_state["user_text_buffer"] = ""
                            
                        for part in model_turn.parts:
                            if getattr(part, 'inline_data', None) and part.inline_data.data:
                                if call_state["interrupting"] or call_state["user_activity_open"]:
                                    continue
                                pcm_8k, call_state["ratecv_state_out"] = audioop.ratecv(
                                    part.inline_data.data, 2, 1, GEMINI_OUTPUT_RATE, PLIVO_SAMPLE_RATE, call_state["ratecv_state_out"]
                                )
                                plivo_ulaw = pcm_to_ulaw(pcm_8k)
                                
                                if plivo_ulaw:
                                    if call_state.get("is_ringing"):
                                        call_state["is_ringing"] = False
                                        if call_state.get("stream_id"):
                                            await plivo_ws.send(json.dumps({
                                                "event": "clearAudio",
                                                "stream_id": call_state["stream_id"]
                                            }))
                                            logger.info("🛑 Gemini generated first audio. Synthetic ringback killed.")
                                    await call_state["plivo_output_queue"].put(plivo_ulaw)
                                    call_state["assistant_speaking"] = True
                                    call_state["awaiting_model"] = False
                                    if call_state.get("closing_audio_phase") and not call_state.get("closing_audio_started"):
                                        call_state["closing_audio_started"] = True
                                        logger.info("🎙️ Closing audio has started streaming from Gemini")
                
                # ==========================================
                # 3. Usage Metadata & Pricing tracking
                # ==========================================
                usage_metadata = getattr(response, 'usage_metadata', None)
                if usage_metadata:
                    total_token_count = getattr(usage_metadata, 'total_token_count', None)
                    if total_token_count is not None:
                        previous_total = call_state["last_usage_total_token_count"]
                        call_state["usage_total_updates"] += 1
                        if previous_total is not None and total_token_count < previous_total:
                            call_state["usage_total_non_monotonic_count"] += 1
                            logger.warning(
                                f"📉 usage_metadata.total_token_count decreased: prev={previous_total}, current={total_token_count}"
                            )
                        else:
                            delta = total_token_count - previous_total if previous_total is not None else None
                            logger.info(
                                f"📊 usage_metadata.total_token_count: current={total_token_count}, prev={previous_total}, delta={delta}"
                            )
                        call_state["last_usage_total_token_count"] = total_token_count

                    if getattr(usage_metadata, 'prompt_tokens_details', None):
                        for detail in usage_metadata.prompt_tokens_details:
                            modality = getattr(detail, 'modality', str(getattr(detail, 'modality', ''))).upper()
                            count = getattr(detail, 'token_count', 0) or 0
                            if "TEXT" in modality:
                                call_state["tokens_text_in"] += count
                            elif "AUDIO" in modality:
                                call_state["tokens_audio_in"] += count
                    
                    if getattr(usage_metadata, 'response_tokens_details', None):
                        for detail in usage_metadata.response_tokens_details:
                            modality = getattr(detail, 'modality', str(getattr(detail, 'modality', ''))).upper()
                            count = getattr(detail, 'token_count', 0) or 0
                            if "TEXT" in modality:
                                call_state["tokens_text_out"] += count
                            elif "AUDIO" in modality:
                                call_state["tokens_audio_out"] += count
                
                # ==========================================
                # 4. Session Resumption & GoAway handling
                # ==========================================
                resumption_update = getattr(response, 'session_resumption_update', None)
                if resumption_update:
                    if getattr(resumption_update, 'resumable', False) and getattr(resumption_update, 'new_handle', None):
                        call_state["session_resumption_handle"] = resumption_update.new_handle
                        logger.info("🔑 Session resumption handle updated")

                go_away = getattr(response, 'go_away', None)
                if go_away is not None:
                    time_left = getattr(go_away, 'time_left', 'unknown')
                    logger.warning(f"⚠️ GoAway received from Gemini. Time left: {time_left}")
                    call_state["go_away_received"] = True
                    emit_call_event(call_state.get("call_uuid"), "gemini_goaway", {"time_left": str(time_left)})
                    # Raise to trigger reconnection before the connection is forcibly closed
                    raise GeminiSessionDisconnected(f"GoAway received, time_left={time_left}")

    except asyncio.CancelledError:
        logger.info("Gemini -> Plivo stream cancelled")
        raise
    except GeminiSessionDisconnected:
        logger.info("🔄 GeminiSessionDisconnected raised, propagating for reconnection")
        raise
    except Exception as e:
        from google.genai import errors as genai_errors
        if isinstance(e, genai_errors.APIError):
            if "1000" in str(e):
                if call_state.get("terminate_session"):
                    logger.info("Gemini Live session closed cleanly (1000 OK) during normal shutdown.")
                    return
                else:
                    logger.warning("⚠️ Gemini Live session closed cleanly (1000 OK) unexpectedly mid-call. Attempting resumption...")
                    raise GeminiSessionDisconnected(f"Unexpected API 1000 closure") from e
            elif "1008" in str(e):
                if call_state.get("session_resumption_handle"):
                    logger.warning(f"⚠️ Gemini Live session closed by API (1008): {e}. Attempting session resumption...")
                    raise GeminiSessionDisconnected(f"API 1008 error: {e}") from e
                else:
                    logger.warning(f"⚠️ Gemini Live session closed by API (1008): {e}. No resumption handle available, ending call.")
                    await terminate_plivo_call(
                        plivo_client,
                        call_state,
                        "Gemini Live session closed with policy error 1008 (no resumption handle)",
                    )
            else:
                logger.error(f"Error in Gemini -> Plivo stream: {e}")
                raise
        else:
            logger.error(f"Error in Gemini -> Plivo stream: {e}")
            raise
    
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

    queue_empty = call_state["plivo_output_queue"].empty()
    playback_idle = not call_state.get("assistant_speaking")
    turn_complete = call_state.get("turn_complete", True)
    deadline = call_state.get("terminal_action_deadline")
    deadline_expired = deadline is not None and time.monotonic() >= deadline

    if not queue_empty or not playback_idle:
        return False
    if not turn_complete and not deadline_expired:
        return False

    call_state["terminal_action_in_progress"] = True
    call_state["ending_call_phase"] = True
    out_buffer.clear()
    await asyncio.sleep(0.2)

    if not call_state["plivo_output_queue"].empty() or call_state["assistant_speaking"]:
        call_state["terminal_action_in_progress"] = False
        call_state["ending_call_phase"] = False
        return False

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
        await plivo_ws.close(1000)
    return True


async def send_plivo_audio(plivo_ws, call_state,session,plivo_client):
    logger.info('Ready to send audio to Plivo')
    out_buffer = bytearray()

    try:
        while True:
            if call_state["terminate_session"]:
                logger.info("Terminating Plivo sender loop")
                break
            try:
                audio = await asyncio.wait_for(call_state["plivo_output_queue"].get(), timeout=PLIVO_SEND_POLL_TIMEOUT)
                out_buffer.extend(audio)

                while len(out_buffer) >= PLIVO_ULAW_CHUNK_SIZE:
                    if call_state["interrupting"] or call_state["user_activity_open"]:
                        out_buffer.clear()
                        break

                    chunk = bytes(out_buffer[:PLIVO_ULAW_CHUNK_SIZE])
                    del out_buffer[:PLIVO_ULAW_CHUNK_SIZE]

                    # Feed the exact audio we are about to play out as the AEC
                    # far-end reference, aligned to real playback time so the
                    # canceller can subtract this signal's echo from the mic.
                    call_state["aec"].add_far_end(ulaw_to_pcm(chunk))

                    # --- TIMING LOGIC: Start time and byte tracking ---
                    if call_state["ai_playback_start_time"] is None:
                        call_state["ai_playback_start_time"] = datetime.now(ist_tz)
                        logger.info(f"🎙️ [TIMING] AI Speech Started playing at: {call_state['ai_playback_start_time'].strftime('%H:%M:%S.%f')[:-3]}")
                        emit_call_event(call_state.get("call_uuid"), "ai_speaking")
                        
                        # Cancel the silence timer when AI starts speaking
                        if call_state.get("silence_timer_task") and not call_state["silence_timer_task"].done():
                            call_state["silence_timer_task"].cancel()
                            call_state["silence_timer_task"] = None
                        
                    call_state["current_utterance_bytes"] += len(chunk)
                    # --------------------------------------------------

                    audio_delta = {
                        "event": "playAudio",
                        "media": {
                            "contentType": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "payload": base64.b64encode(chunk).decode("utf-8"),
                        }
                    }
                    await plivo_ws.send(json.dumps(audio_delta))

            except asyncio.TimeoutError:
                if len(out_buffer) < PLIVO_ULAW_CHUNK_SIZE and call_state["plivo_output_queue"].empty():
                    if call_state["ai_playback_start_time"] is not None and call_state["current_utterance_bytes"] > 0:
                        duration_seconds = call_state["current_utterance_bytes"] / 8000.0
                        expected_end_time = call_state["ai_playback_start_time"] + timedelta(seconds=duration_seconds)
                        now = datetime.now(ist_tz)

                        if now < expected_end_time:
                            continue

                        logger.info(
                            f"AI speech playback ended at {now.strftime('%H:%M:%S.%f')[:-3]} "
                            f"(calculated duration: {duration_seconds:.2f}s)"
                        )
                        emit_call_event(call_state.get("call_uuid"), "ai_done")

                        if not call_state.get("greeting_completed", False):
                            logger.info("Initial greeting complete. Microphone is now live.")
                            call_state["greeting_completed"] = True
                            emit_call_event(call_state.get("call_uuid"), "call_connected")

                        call_state["ai_playback_start_time"] = None
                        call_state["current_utterance_bytes"] = 0
                        call_state["assistant_speaking"] = False
                        call_state["interrupting"] = False

                        if (
                            call_state.get("turn_complete", True)
                            and not call_state["pending_end_call"]
                            and not call_state["pending_transfer_call"]
                            and not call_state["closing_audio_phase"]
                        ):
                            if call_state.get("silence_timer_task") and not call_state["silence_timer_task"].done():
                                call_state["silence_timer_task"].cancel()
                            call_state["silence_timer_task"] = asyncio.create_task(
                                silence_watchdog(
                                    session,
                                    call_state,
                                    delay=SILENCE_FOLLOWUP_SECONDS,
                                )
                            )
                    else:
                        call_state["assistant_speaking"] = False
                        call_state["interrupting"] = False

                    terminal_executed = await execute_pending_terminal_action(
                        plivo_ws,
                        call_state,
                        plivo_client,
                        out_buffer,
                    )
                    if terminal_executed:
                        break

                continue

    except Exception as e:
        logger.error(f"Error in send_plivo_audio: {e}")
        raise

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
