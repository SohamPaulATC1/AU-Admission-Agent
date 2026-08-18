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
from pyrnnoise import RNNoise
import chromadb
import numpy as np
import bisect

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

logger.info("Initializing ChromaDB connection...")
chroma_client = chromadb.PersistentClient(path="./senco_chroma_db")
senco_collection = chroma_client.get_or_create_collection(name="senco_stores")
logger.info("✅ ChromaDB ready for searches!")

logger.info("Loading and sorting Pincode Database...")
try:
    with open("senco_llm4.json", "r", encoding="utf-8") as f:
        stores_pincode_data = json.load(f)
    
    # Sort once at startup
    stores_pincode_data.sort(key=lambda x: int(x["pincode"]))
    
    # Extract pincodes into a flat list for binary search
    fast_pincode_index = [int(store["pincode"]) for store in stores_pincode_data]
    logger.info(f"✅ Loaded {len(stores_pincode_data)} stores for Pincode tool!")
except Exception as e:
    logger.error(f"❌ Failed to load senco_llm2.json: {e}")
    stores_pincode_data = []
    fast_pincode_index = []

# --- PRICING CONSTANTS (Per 1M Tokens) ---
PRICE_TEXT_INPUT = 0.50
PRICE_TEXT_OUTPUT = 2.00
PRICE_AUDIO_INPUT = 3.00
PRICE_AUDIO_OUTPUT = 12.00

LIVE_API_KEY = os.getenv('GOOGLE_API_KEY')
GEMINI_MODEL = "gemini-live-2.5-flash-native-audio"
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL')
HUMAN_TRANSFER_NUMBER = "+918335027643"
PLIVO_PHONE_NUMBER = os.getenv('FROM_NUMBER')
transfer_context_store = {}

PORT = 8008

# Telephony / model audio formats
PLIVO_SAMPLE_RATE = 8000
GEMINI_INPUT_RATE = 16000
GEMINI_OUTPUT_RATE = 24000

# Audio chunking
PLIVO_ULAW_CHUNK_SIZE = 160          # 20 ms @ 8kHz μ-law
GEMINI_PCM_CHUNK_SIZE = 640          # 20 ms @ 16kHz PCM16 = 320 samples = 640 bytes
PREROLL_MAX_BYTES_PCM8 = 3200        # ~200 ms @ 8kHz PCM16

VAD_THRESHOLD = 0.75

PREROLL_MAX_BYTES_PCM16 = 6400       # ~200 ms @ 16kHz PCM16 (Replacing PREROLL_MAX_BYTES_PCM8)

# DSP Constants
AGC_TARGET_DB = -12.0
AGC_MAX_GAIN_DB = 30.0               # Tuned down to prevent telephony artifact amplification
AGC_NOISE_GATE_DB = -50.0
AGC_SMOOTHING_ALPHA = 0.08         # Slow alpha to prevent volume pumping

#total_cost = 0

with open("prompt.txt", "r", encoding="utf-8") as f:
    RAW_SYSTEM_PROMPT = f.read()
    
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
    description="Ends the call immediately when the user says goodbye or wants to end the conversation."
)

transfer_call_tool = types.FunctionDeclaration(
    name="transferCall",
    description=(
        "Handovers the call to a human on the admin side for resolving issues the AI agent couldn't."
    ),
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

get_store_details_tool = types.FunctionDeclaration(
    name="getStoreDetails",
    description=(
        "Fetches Senco Gold store addresses, names, and phone numbers. "
        "Call this tool whenever the user asks about store locations, addresses, or contact information."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "search_query": {
                "type": "STRING",
                "description": "The location to search for, e.g., 'Dhubri Assam', 'stores in Kolkata', or 'Bettiah Bihar'."
            }
        },
        "required": ["search_query"]
    }
)

get_nearest_stores_by_pincode_tool = types.FunctionDeclaration(
    name="getNearestStoresByPincode",
    description=(
        "Fetches the top 5 nearest Senco Gold store addresses and details based on a 6-digit Indian pincode (postal code). "
        "Call this tool whenever the user provides a pincode to find stores near them."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "pincode": {
                "type": "INTEGER",
                "description": "The 6-digit Indian pincode provided by the user."
            }
        },
        "required": ["pincode"]
    }
)

LOCAL_GEMINI_TOOLS = [{"function_declarations": [end_call_tool, transfer_call_tool, get_store_details_tool,get_nearest_stores_by_pincode_tool]}]
    
SILENCE_FOLLOWUP_PROMPT = (
    "The user has been silent for a few seconds after you spoke with them last."
    "Please briefly and politely re-engage them in your existing Bengali-English style. "
    "Keep it short, natural, non-pushy, and context-aware."
)

async def silence_watchdog(session, call_state, delay=6.0):
    try:
        await asyncio.sleep(delay)
        if not call_state.get("terminate_session") and not call_state.get("user_activity_open"):
            logger.info(f"⏱️ [SILENCE] User silent for {delay} seconds. Prompting AI to re-engage.")
            await session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=SILENCE_FOLLOWUP_PROMPT)],
                ),
                turn_complete=True,
            )
    except asyncio.CancelledError:
        pass
    
def calculate_rms_db(pcm_data):
    """Converts int16 PCM to float32, calculates RMS in dB, and returns both."""
    samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
    rms_val = np.sqrt(np.mean(samples**2) + 1e-12)
    rms_db = 20 * np.log10(rms_val + 1e-12)
    return rms_db, samples

app = Quart(__name__)

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

    # Retrieve and remove the stored context
    context = transfer_context_store.pop(call_uuid, {})
    if not context:
        logger.warning(f"⚠️ No transfer context found for call_uuid={call_uuid}! Proceeding with empty context.")
        
    summary = context.get("summary", "")
    original_from = context.get("from_number", from_number)

    logger.info(f"📋 Retrieved summary for re-entry: {summary}")

    import urllib.parse
    encoded_summary  = urllib.parse.quote(summary)
    encoded_from     = urllib.parse.quote(original_from)
    
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
            wss://{ws_host}/media-stream?from_number={encoded_from}&amp;transfer_summary={encoded_summary}&amp;dial_status={encoded_status}
        </Stream>
    </Response>
    """
    return Response(xml_data, mimetype='application/xml')

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
    
    user_name = websocket.args.get("user_name", "Unknown")
    phone_number = websocket.args.get("phone_number", "Unknown")
    
    transfer_summary_raw = websocket.args.get("transfer_summary", "")
    dial_status      = websocket.args.get("dial_status", "")
    is_returning_from_transfer = bool(transfer_summary_raw)
    
    call_summary = ""
    user_language = "Bengali"
    
    if is_returning_from_transfer:
        try:
            parsed_summary = json.loads(transfer_summary_raw)
            call_summary = parsed_summary.get("call_summary", "")
            user_language = parsed_summary.get("language", "Bengali")
        except json.JSONDecodeError:
            # Fallback just in case the raw string isn't valid JSON
            logger.warning("Could not parse transfer_summary as JSON. Falling back to raw string.")
            call_summary = transfer_summary_raw
    
    logger.info(f'Client connected to Quart WebSocket. Caller: {phone_number}')
    plivo_ws = websocket
    
     # Dictionary to maintain state without using Python global variables
    call_state = {
        "from_number": phone_number,
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
        "interrupting": False,
        
        # AI speech timing trackers
        "ai_playback_start_time": None,
        "current_utterance_bytes": 0,
        "turn_complete": True,
        
        # silence handling
        "silence_timer_task": None,
        
        # deferred end-call control
        "pending_end_call": False,
        "ending_call_phase": False,
        "closing_audio_phase": False,
        "closing_audio_started": False,
        "end_call_tool_executed": False,
        "terminate_session": False,
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
    disclaimer_task = asyncio.create_task(play_disclaimer(plivo_ws, call_state, disclaimer_finished))
    
    # Initialize the new SDK client
    client = genai.Client(
        vertexai=True, 
        project=os.getenv('GOOGLE_CLOUD_PROJECT'), 
        location=os.getenv('GOOGLE_CLOUD_LOCATION')
    )
    # client = genai.Client(
    #     api_key=LIVE_API_KEY
    # )

    try:
        current_time = get_indian_time()
        try:
            system_instruction_text = f"Current Date and Time (India IST): {current_time}\n\n" + RAW_SYSTEM_PROMPT.format(**locals())
            
            #logger.info(system_instruction_text)
        except KeyError as e:
            logger.error(f"⚠️ Missing a placeholder variable in the prompt file: {e}")
            system_instruction_text = f"Current Date and Time (India IST): {current_time}\n\n" + RAW_SYSTEM_PROMPT
        
        if is_returning_from_transfer:
            logger.info(f"🔄 Re-entry after human transfer. DialStatus={dial_status}")
            user_context = f"The user called from {phone_number}."
                
            if dial_status == "completed":
                initial_prompt = f"""
                System Event: The user was previously speaking with you (Sia), was then transferred to a human agent,
                the user and human agent are done now and the human agent has put down the call. The user is back on the line with you.

                {user_context}

                The user's preferred language is: **{user_language}**

                Here is a summary of the conversation before the transfer: 
                {call_summary}

                Instructions:
                - Greet the user UNMISTAKABLY in {user_language} by saying - "Looks like you have spoken with our store executive , How can I help you now?" in {user_language}
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
                System Event: The user was previously speaking with you (Sia) and requested to be transferred
                to a human agent. However, the human agent was unavailable (DialStatus: {dial_status}).
                The user is back on the line with you.

                {user_context}

                The user's preferred language is: **{user_language}**

                Here is a summary of the conversation before the transfer: 
                {call_summary}

                Instructions:
                - Apologise UNMISTAKABLY in {user_language} by saying exactly - "Sorry looks like our store executive couldn't connect , let me help you out in case." in {user_language}.
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
            * **Greeting:** Start every call with "Namaskhar {user_name}" in English, followed by: "I’m Sia, calling from Senco Gold and Diamonds, Bowbazar Kolkata showroom. Do you want to continue speaking in English or switch to Hindi or Bengali."
            """
        
        # Configure the Live Session
        config = types.LiveConnectConfig(
            system_instruction=types.Content(parts=[
                types.Part.from_text(text=system_instruction_text)
            ]),
            tools=LOCAL_GEMINI_TOOLS,
            temperature = 0.2,
            response_modalities=["AUDIO"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            thinking_config=types.ThinkingConfig(
                thinking_budget=0,
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
            context_window_compression={
                "trigger_tokens": 10000, 
                "sliding_window": {"target_tokens": 2048}
            }
        )
        
        async with client.aio.live.connect(model=GEMINI_MODEL, config=config) as session:
            logger.info('Connected to Google GenAI Live API')
            
            # --- 1. WAIT FOR DISCLAIMER ---
            logger.info("⏳ Waiting for disclaimer audio to finish playing...")
            await disclaimer_finished.wait()
            
            # --- 2. INJECT INITIAL CONTEXT & TRIGGER GREETING ---
            logger.info("🟢 Disclaimer done. Sending initial context to trigger AI greeting...")
            await session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=initial_prompt)],
                ),
                turn_complete=True,
            )

            # Run sending and receiving concurrently
            plivo_to_gemini_task = asyncio.create_task(
                stream_plivo_to_gemini(plivo_ws, session, call_state)
            )
            gemini_to_plivo_task = asyncio.create_task(
                stream_gemini_to_plivo(session, plivo_ws, call_state ,plivo_client)
            )
            plivo_sender_task = asyncio.create_task(
                send_plivo_audio(plivo_ws, call_state , session,plivo_client)
            )

            try :
                await asyncio.gather(plivo_to_gemini_task, gemini_to_plivo_task, plivo_sender_task, disclaimer_task)
            finally:
                if call_state.get("silence_timer_task") and not call_state["silence_timer_task"].done():
                    call_state["silence_timer_task"].cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await call_state["silence_timer_task"]
                        
                for task in [plivo_to_gemini_task, gemini_to_plivo_task , plivo_sender_task]:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                            
                #--- Calculate and log pricing ---
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

    except asyncio.CancelledError:
        logger.info('Client disconnected')
        return
    except Exception as e:
        logger.error("🚨 CRITICAL ERROR: Live API TaskGroup Crashed!")
        logger.error(traceback.format_exc())
    finally:
        if 'call_state' in locals():
            call_state["playing_disclaimer"] = False
        if 'disclaimer_task' in locals() and not disclaimer_task.done():
            disclaimer_task.cancel()

async def stream_plivo_to_gemini(plivo_ws, session, call_state):
    logger.info('Ready to stream audio from Plivo to Gemini')

    try:
        while True:
            if call_state["terminate_session"]:
                logger.info("🎬 Terminating Plivo -> Gemini loop")
                break
            message = await plivo_ws.receive()
            data = json.loads(message)

            if data['event'] == 'start':
                logger.info('Plivo Audio stream has started')
                logger.info(f"Plivo start payload: {json.dumps(data['start'])}")

                call_state["stream_id"] = data['start']['streamId']
                call_state["call_uuid"] = (
                    data['start'].get('callId')
                    or data['start'].get('callUuid')
                    or data['start'].get('callUUID')
                    or data['start'].get('call_id')
                    or data['start'].get('call_uuid')
                )
                call_state["host"] = data['start'].get('customParameters', {}).get('host') or os.getenv("PUBLIC_HOST")

                logger.info(f'Stream ID: {call_state["stream_id"]}')
                logger.info(f'Call UUID: {call_state["call_uuid"]}')
                logger.info(f'From Number: {call_state["from_number"]}')
                logger.info(f'PUBLIC_BASE_URL: {call_state["public_base_url"]}')

            elif data['event'] == 'media':
                payload = data.get('media', {}).get('payload')
                if not payload:
                    continue
                
                # Ignore user audio if we are ending the call OR if the disclaimer is actively playing
                if call_state.get("closing_audio_phase") or call_state.get("playing_disclaimer"):
                    continue

                mulaw_chunk = base64.b64decode(payload)
                pcm_8k = ulaw_to_pcm(mulaw_chunk)

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

                SLOW_ALPHA = 0.02 
                call_state["agc_current_gain_lin"] = (SLOW_ALPHA * target_gain_lin) + ((1.0 - SLOW_ALPHA) * call_state["agc_current_gain_lin"])

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
                
                if call_state["chunk_count"] % 50 == 0:
                    current_gain_db_log = 20 * np.log10(call_state["agc_current_gain_lin"] + 1e-12)
                    logger.info(f"🔊 [AGC] Inbound RMS: {rms_db:.1f} dB | Gain: +{current_gain_db_log:.1f} dB")

                noise_end_time = time.perf_counter()
                
                avg_prob = float(np.mean(speech_probs)) if speech_probs else 0.0
                speech_started = False
                speech_ended = False
                
                if avg_prob > VAD_THRESHOLD:  # Threshold for confident speech
                    call_state["rnnoise_speech_count"] += 1
                    call_state["rnnoise_silence_frames"] = 0
                    if call_state["rnnoise_speech_count"] >= 3 and not call_state["is_speaking"]:
                        call_state["is_speaking"] = True
                        speech_started = True
                else:
                    call_state["rnnoise_speech_count"] = 0
                    if call_state["is_speaking"]:
                        call_state["rnnoise_silence_frames"] += 1
                        if call_state["rnnoise_silence_frames"] >= 15: # ~300ms silence
                            call_state["is_speaking"] = False
                            speech_ended = True
                            
                call_state["chunk_count"] += 1
                if call_state["chunk_count"] % 50 == 0:
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
                            await session.send_realtime_input(
                                activity_start=types.ActivityStart()
                            )
                            call_state["user_activity_open"] = True
                            call_state["awaiting_model"] = False
                            call_state["turn_complete"] = False
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
                    call_state["interrupting"] = False
                    logger.info("⏹️ Sent activityEnd to Gemini")

            elif data['event'] == 'stop':
                logger.info('Plivo stream stopped')
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
                    
                    try:
                        function_responses_to_send = []
                    
                        for call in response.tool_call.function_calls:
                            logger.info(f"\n[⚙️ Gemini requested tool execution: {call.name}]")
                            args_dict = dict(call.args) if call.args else {}
                            logger.info(f"Arguments: {args_dict}")

                            try:
                                if call.name == "endCall":
                                    end_call_summary = args_dict.get("summary_of_whole_call", "")
                                    call_state["end_call_summary"] = end_call_summary
                                    logger.info(f"📋 End-call summary captured from Gemini: {end_call_summary}")
                                    call_state["pending_end_call"] = True
                                    call_state["closing_audio_phase"] = True
                                    call_state["end_call_tool_executed"] = False
                                    call_state["terminate_session"] = False
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
                                    call_state["transfer_call_tool_executed"] = False
                                    call_state["terminate_session"] = False
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
                                    
                                elif call.name == "getStoreDetails":
                                    search_query = args_dict.get("search_query", "")
                                    logger.info(f"🔍 Searching ChromaDB for: {search_query}")
                                    
                                    try:
                                        # Run synchronous ChromaDB query in a separate thread so it doesn't block audio
                                        db_results = await asyncio.to_thread(
                                            senco_collection.query,
                                            query_texts=[search_query],
                                            n_results=3
                                        )
                                        
                                        # Stitch text and metadata together exactly like we did previously
                                        if db_results['documents'] and db_results['documents'][0]:
                                            context_chunks = []
                                            for i in range(len(db_results['documents'][0])):
                                                doc_text  = db_results['documents'][0][i]
                                                metadata  = db_results['metadatas'][0][i]

                                                # Extract clean address from document string
                                                address = doc_text.split("Details:")[0].strip()
                                                
                                                #Clean pincode
                                                raw_pincode = metadata.get('pincode', '')
                                                formatted_pincode = format_digits(raw_pincode)
                                                
                                                #Clean phone numbers
                                                raw_phones = metadata.get('phones', '')
                                                formatted_phones = " | ".join(
                                                    format_digits(p.strip()) 
                                                    for p in raw_phones.split(",") 
                                                    if p.strip()
                                                )

                                                chunk = (
                                                    f"Store Name: {metadata.get('store_name', 'N/A')}\n"
                                                    f"Address: {address}\n"
                                                    f"State: {metadata.get('state', 'N/A')}\n"
                                                    f"District: {metadata.get('district', 'N/A')}\n"
                                                    f"Pincode: {formatted_pincode}\n"
                                                    f"Phones: {formatted_phones}"
                                                )

                                                logger.info(f"Store Result {i+1}:\n{chunk}")
                                                context_chunks.append(chunk)

                                            retrieved_context = "\n\n---\n\n".join(context_chunks)
                                        else:
                                            retrieved_context = "No stores found matching that location."
                                            
                                        logger.info(f"✅ ChromaDB returned {len(db_results['documents'][0]) if db_results['documents'] else 0} results.")
                                        
                                        # Send the data back to Gemini
                                        function_responses_to_send.append(
                                            types.FunctionResponse(
                                                id=call.id,
                                                name=call.name,
                                                response={"result": retrieved_context}
                                            )
                                        )
                                    except Exception as db_err:
                                        logger.error(f"❌ ChromaDB Search Failed: {db_err}")
                                        function_responses_to_send.append(
                                            types.FunctionResponse(
                                                id=call.id,
                                                name=call.name,
                                                response={"error": "Database search failed. Tell the user you are currently unable to fetch the store details."}
                                            )
                                        )
                                        
                                elif call.name == "getNearestStoresByPincode":
                                    user_pincode = args_dict.get("pincode")
                                    logger.info(f"📍 Finding nearest stores for pincode: {user_pincode}")

                                    try:
                                        user_pincode = int(user_pincode)
                                        result = []
                                        
                                        # Binary search position
                                        pos = bisect.bisect_left(fast_pincode_index, user_pincode)
                                        left = pos - 1
                                        right = pos
                                        
                                        # Collect exact matches first
                                        while right < len(stores_pincode_data) and fast_pincode_index[right] == user_pincode:
                                            result.append(stores_pincode_data[right])
                                            right += 1
                                        
                                        while left >= 0 and fast_pincode_index[left] == user_pincode:
                                            result.append(stores_pincode_data[left])
                                            left -= 1
                                        
                                        # Expand to get remaining closest based on numerical difference
                                        while len(result) < 5 and (left >= 0 or right < len(stores_pincode_data)):
                                            if left < 0:
                                                result.append(stores_pincode_data[right])
                                                right += 1
                                            elif right >= len(stores_pincode_data):
                                                result.append(stores_pincode_data[left])
                                                left -= 1
                                            else:
                                                if abs(fast_pincode_index[left] - user_pincode) <= abs(fast_pincode_index[right] - user_pincode):
                                                    result.append(stores_pincode_data[left])
                                                    left -= 1
                                                else:
                                                    result.append(stores_pincode_data[right])
                                                    right += 1

                                        top_5_stores = result[:5]
                                        
                                        # Format the results into a clean string for Gemini to read
                                        if top_5_stores:
                                            formatted_stores = []
                                            # ✅ New — structured formatting with format_digits applied
                                            for idx, store in enumerate(top_5_stores):
                                                meta = store.get("metadata", {})

                                                raw_pincode       = str(store.get("pincode", ""))
                                                formatted_pincode = format_digits(raw_pincode) if raw_pincode else "N/A"

                                                # phones is a list e.g. ["9147106932", "9147132351"]
                                                phones_list      = meta.get("phones", [])
                                                formatted_phones = " | ".join(
                                                    format_digits(p.strip()) for p in phones_list if p.strip()
                                                ) if phones_list else "N/A"

                                                chunk = (
                                                    f"Store {idx+1}:\n"
                                                    f"Store Name: {meta.get('store_name', 'N/A')}\n"
                                                    f"Address: {store.get('address', 'N/A')}\n"
                                                    f"State: {meta.get('state', 'N/A')}\n"
                                                    f"District: {meta.get('district', 'N/A')}\n"
                                                    f"Pincode: {formatted_pincode}\n"
                                                    f"Phones: {formatted_phones}"
                                                )

                                                logger.info(f"Store Result {idx+1}:\n{chunk}")  # ✅ fixed i+1 → idx+1
                                                formatted_stores.append(chunk)
                                            
                                            retrieved_context = "\n\n".join(formatted_stores)
                                        else:
                                            retrieved_context = "No stores found or database empty."

                                        logger.info(f"✅ Found {len(top_5_stores)} nearest stores for pincode {user_pincode}.")

                                        # Send the result back to Gemini
                                        function_responses_to_send.append(
                                            types.FunctionResponse(
                                                id=call.id,
                                                name=call.name,
                                                response={"result": retrieved_context}
                                            )
                                        )

                                    except Exception as e:
                                        logger.error(f"❌ Pincode Search Failed: {e}")
                                        function_responses_to_send.append(
                                            types.FunctionResponse(
                                                id=call.id,
                                                name=call.name,
                                                response={"error": "Failed to search by pincode. Tell the user you are currently unable to fetch the details."}
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
                            
                        if function_responses_to_send:
                            await session.send_tool_response(
                                function_responses=function_responses_to_send
                            )
                            
                    finally:
                        call_state["tool_call_in_progress"] = False

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
                        call_state["interrupting"] = False
                        call_state["pending_end_call"] = False
                        call_state["pending_transfer_call"] = False
                        call_state["ending_call_phase"] = False
                        call_state["closing_audio_phase"] = False
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
                            logger.info(f"🤖 [GEMINI]: {call_state['ai_text_buffer'].strip()}")
                            call_state["conversation_log"].append({
                                "role": "agent",
                                "text": call_state["ai_text_buffer"].strip()
                            })
                            call_state["ai_text_buffer"] = ""
                    
                    model_turn = getattr(server_content, 'model_turn', None)
                    #logger.info("⚡ Gemini Audio Chunk Received!")
                    if model_turn is not None:
                        
                        # Flush the completed user sentence to the logs now.
                        if call_state["user_text_buffer"].strip() and not call_state["assistant_speaking"]:
                            logger.info(f"🗣️ [USER]: {call_state['user_text_buffer'].strip()}")
                            call_state["conversation_log"].append({
                                "role": "user",
                                "text": call_state["user_text_buffer"].strip()
                            })
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
    except asyncio.CancelledError:
        logger.info("Gemini -> Plivo stream cancelled")
        raise
    except Exception as e:
        from google.genai import errors as genai_errors
        if isinstance(e, genai_errors.APIError) and "1008" in str(e):
            logger.warning(f"⚠️ Gemini Live session closed by API (1008): {e}. Ending call gracefully.")
            call_state["terminate_session"] = True
            if call_state.get("call_uuid"):
                try:
                    await asyncio.to_thread(
                        plivo_client.calls.delete,
                        call_uuid=call_state["call_uuid"]
                    )
                    logger.info(f"📞 Call hung up after Gemini 1008 error for call_uuid={call_state['call_uuid']}")
                except Exception as hangup_err:
                    logger.error(f"❌ Failed to hang up call after 1008 error: {hangup_err}")
        else:
            logger.error(f"Error in Gemini -> Plivo stream: {e}")
            raise
    
async def send_plivo_audio(plivo_ws, call_state,session,plivo_client):
    logger.info('Ready to send audio to Plivo')
    out_buffer = bytearray()

    try:
        while True:
            if call_state["terminate_session"]:
                logger.info("Terminating Plivo sender loop")
                break
            try:
                audio = await asyncio.wait_for(call_state["plivo_output_queue"].get(), timeout=0.1)
                out_buffer.extend(audio)

                while len(out_buffer) >= PLIVO_ULAW_CHUNK_SIZE:
                    if call_state["interrupting"] or call_state["user_activity_open"]:
                        out_buffer.clear()
                        break

                    chunk = bytes(out_buffer[:PLIVO_ULAW_CHUNK_SIZE])
                    del out_buffer[:PLIVO_ULAW_CHUNK_SIZE]
                    
                    # --- TIMING LOGIC: Start time and byte tracking ---
                    if call_state["ai_playback_start_time"] is None:
                        call_state["ai_playback_start_time"] = datetime.now(ist_tz)
                        logger.info(f"🎙️ [TIMING] AI Speech Started playing at: {call_state['ai_playback_start_time'].strftime('%H:%M:%S.%f')[:-3]}")
                        
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
                    if not call_state.get("turn_complete", True):
                        continue
                    
                    # --- TIMING LOGIC: Natural End Calculation ---
                    if call_state["ai_playback_start_time"] is not None and call_state["current_utterance_bytes"] > 0:
                        # 8000 bytes of mu-law = 1 second of audio
                        duration_seconds = call_state["current_utterance_bytes"] / 8000.0
                        expected_end_time = call_state["ai_playback_start_time"] + timedelta(seconds=duration_seconds)
                        now = datetime.now(ist_tz)
                        
                        # NEW: Check if real-time has caught up to the audio duration
                        if now < expected_end_time:
                            # Plivo is still playing the audio buffer to the user!
                            # We just continue the loop and wait. 
                            continue 
                        
                        # Only execute this once the real-time playback has actually finished
                        logger.info(f"🎙️ [TIMING] AI Speech Natural End at: {now.strftime('%H:%M:%S.%f')[:-3]} (Calculated Duration: {duration_seconds:.2f}s)")
                        
                        if not call_state.get("greeting_completed", False):
                            logger.info("🔓 Initial greeting complete. Microphone is now LIVE!")
                            call_state["greeting_completed"] = True
                        
                        # Reset trackers for the next conversational turn
                        call_state["ai_playback_start_time"] = None
                        call_state["current_utterance_bytes"] = 0
                        call_state["assistant_speaking"] = False
                        call_state["interrupting"] = False
                        
                        # Start the silence watchdog timer (e.g., 8 seconds delay)
                        if not call_state["pending_end_call"] and not call_state["pending_transfer_call"] and not call_state["closing_audio_phase"]:
                            if call_state.get("silence_timer_task") and not call_state["silence_timer_task"].done():
                                call_state["silence_timer_task"].cancel()
                            call_state["silence_timer_task"] = asyncio.create_task(
                                silence_watchdog(session, call_state, delay=8.0)
                            )
                    else:
                        # Fallback if trackers were wiped (e.g., by user barge-in)
                        call_state["assistant_speaking"] = False
                        call_state["interrupting"] = False
                    # ---------------------------------------------
                    
                    if (
                        call_state["pending_end_call"]
                        and call_state.get("closing_audio_started")
                        and not call_state["user_activity_open"]
                        and not call_state["end_call_tool_executed"]
                        and not call_state["assistant_speaking"]
                    ):
                        logger.info("⏳ Hangup condition met. Waiting 0.5s to ensure audio buffer is completely flushed...")
                        call_state["ending_call_phase"] = True
                        await asyncio.sleep(0.4)
                        if not call_state["plivo_output_queue"].empty() or call_state["assistant_speaking"] or not call_state.get("turn_complete", True):
                            logger.info("🚫 Hangup aborted: Late audio chunks arrived in the queue. Resuming playback...")
                            continue
                        
                        logger.info("📴 Audio fully cleared; executing Plivo hangup now")
                        call_state["end_call_tool_executed"] = True

                        if call_state.get("call_uuid"):
                            try:
                                await asyncio.to_thread(
                                    plivo_client.calls.delete,
                                    call_uuid=call_state["call_uuid"],
                                )
                                logger.info(f"📞 Plivo call explicitly terminated for call_uuid={call_state['call_uuid']}")
                            except Exception as e:
                                logger.error(f"❌ Failed to hang up call: {e}")

                        call_state["terminate_session"] = True
                        call_state["pending_end_call"] = False
                        call_state["ending_call_phase"] = False
                        break

                    if (
                        call_state["pending_transfer_call"]
                        and call_state.get("closing_audio_started")
                        and not call_state["user_activity_open"]
                        and not call_state["transfer_call_tool_executed"]
                        and not call_state["assistant_speaking"] 
                    ):
                        logger.info("⏳ Transfer condition met. Waiting 0.5s to ensure audio buffer is completely flushed...")
                        call_state["ending_call_phase"] = True 
                        await asyncio.sleep(0.4)
                        
                        if not call_state["plivo_output_queue"].empty() or call_state["assistant_speaking"] or not call_state.get("turn_complete", True):
                            logger.info("🚫 Transfer aborted: Late audio chunks arrived in the queue. Resuming playback...")
                            continue

                        logger.info("🔀 Audio fully cleared; executing Plivo transfer now")
                        call_state["transfer_call_tool_executed"] = True

                        if call_state.get("call_uuid") and call_state.get("public_base_url"):
                            import urllib.parse
                            
                            transfer_context_store[call_state["call_uuid"]] = {
                                "summary": call_state.get("transfer_summary", ""),
                                "from_number": call_state["from_number"],
                            }
                            logger.info(f"💾 Transfer context stored for call_uuid={call_state['call_uuid']}")
                            
                            transfer_url = f"{call_state['public_base_url']}/transfer.xml?call_uuid={urllib.parse.quote(call_state['call_uuid'])}"
                            
                            try:
                                transfer_response = await asyncio.to_thread(
                                    plivo_client.calls.transfer,
                                    call_uuid=call_state["call_uuid"],
                                    legs="aleg",
                                    aleg_url=transfer_url,
                                    aleg_method="GET"
                                )
                                logger.info(f"📞 Plivo call transferred via API for call_uuid={call_state['call_uuid']}")
                                call_state["ending_call_phase"] = True
                                logger.info("⏳ Waiting for Plivo to redirect call before closing WebSocket...")
                                await asyncio.sleep(3.0)
                                logger.info("🔌 Closing WebSocket to release Plivo Stream for transfer...")
                                await plivo_ws.close(1000)
                            except Exception as e:
                                logger.error(f"❌ Failed to transfer call: {e}")

                        call_state["pending_transfer_call"] = False
                        call_state["terminate_session"] = True
                        break      

                continue

    except Exception as e:
        print(f"Error in send_plivo_audio: {e}")
        raise

@app.route("/trigger-call", methods=["POST"])
async def trigger_call():
    data = await request.get_json()
    
    target_user_name = data.get("user_name", "Unknown")
    target_phone_number = data.get("phone_number")
    
    if not target_phone_number:
        return {"status": "error", "message": "Phone number is required"}, 400

    import urllib.parse
    encoded_name = urllib.parse.quote(target_user_name)
    encoded_phone = urllib.parse.quote(target_phone_number)
    
    answer_url = f"{PUBLIC_BASE_URL}/outbound-webhook?user_name={encoded_name}&phone_number={encoded_phone}"
    
    try:
        logger.info(f"Initiating outbound call to {target_phone_number} via Dashboard...")
        call_made = plivo_client.calls.create(
            from_= PLIVO_PHONE_NUMBER,
            to_="+91"+target_phone_number,
            answer_url=answer_url,
            answer_method='GET'
        )
        logger.info(f"✅ Outbound call successfully queued: {call_made}")
        return {"status": "success", "message": "Call queued", "call_uuid": str(getattr(call_made, 'request_uuid', call_made))}, 200
    except Exception as e:
        logger.error(f"❌ Failed to initiate outbound call: {e}")
        return {"status": "error", "message": str(e)}, 500

if __name__ == "__main__":
    logger.info(f'Starting the Quart Server on Port {PORT} (Waiting for Dashboard trigger...)')
    app.run(port=PORT)
