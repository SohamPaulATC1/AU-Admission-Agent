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
import hashlib
import unicodedata
import regex
import collections
import bargein
import course_catalog
from pyrnnoise import RNNoise
import numpy as np
import urllib.parse

# Import the new GenAI SDK
from google import genai
from google.genai import types

load_dotenv()

AEC_IMPL = os.getenv("AEC_IMPL", "aec1").strip().lower()
if AEC_IMPL == "aec":
    from aec import AcousticEchoCanceller
elif AEC_IMPL == "aec1":
    from aec1 import AcousticEchoCanceller
else:
    raise ValueError(f"AEC_IMPL must be 'aec' or 'aec1', got {AEC_IMPL!r}")

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
PRICE_TEXT_INPUT = float(os.getenv("PRICE_TEXT_INPUT", "0.75"))
PRICE_TEXT_OUTPUT = float(os.getenv("PRICE_TEXT_OUTPUT", "4.50"))
PRICE_AUDIO_INPUT = float(os.getenv("PRICE_AUDIO_INPUT", "3.00"))
PRICE_AUDIO_OUTPUT = float(os.getenv("PRICE_AUDIO_OUTPUT", "12.00"))

LIVE_API_KEY = os.getenv('GOOGLE_API_KEY')
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-live")
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL')
HUMAN_TRANSFER_NUMBER = os.getenv('HUMAN_TRANSFER_NUMBER')
PLIVO_PHONE_NUMBER = os.getenv('FROM_NUMBER')

# Select the Gemini backend: "vertex" (production) or "studio" (development fallback).
GEMINI_BACKEND = os.getenv("GEMINI_BACKEND", "vertex").strip().lower()
if GEMINI_BACKEND not in ("vertex", "studio"):
    raise ValueError(f"GEMINI_BACKEND must be 'vertex' or 'studio', got {GEMINI_BACKEND!r}")
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "silver-shift-490819-k0")
# Vertex location. us-central1: eu stalled mid-turn on live calls (TEST6, 2026-10-05); eu stays available via env.
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1").strip().lower()
VERTEX_CREDENTIALS_PATH = os.getenv(
    "VERTEX_CREDENTIALS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "silver-shift-490819-k0-1b9ed54b2663.json"),
)
# Refresh the access token (1 h lifetime) when less than this much is left.
VERTEX_TOKEN_REFRESH_MARGIN_SECONDS = 300

PORT = int(os.getenv("PORT", "8000"))

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

# --- Truncation and Commit Gate Settings ---
# ANOMALOUS_TRUNCATION_MS: Short barge-ins below this threshold are classified as anomalous.
# GRAPHEME_COMMIT_MS: Amount of audio withheld to prevent fragmented stutters during a barge-in.
ANOMALOUS_TRUNCATION_MS = 350
GRAPHEME_COMMIT_MS = 240

# --- Playback Measurement Setting ---
# Determines how we measure what the caller actually heard to properly classify anomalies.
# "wallclock" uses actual time passed, avoiding discrepancies from buffered audio.
TRUNCATION_CLOCK = os.getenv("TRUNCATION_CLOCK", "wallclock")   # "wallclock" | "bytes"

# Enable to delay regenerated audio to avoid stutter gaps. Default OFF.
COMMIT_GATE_ENABLED = os.getenv("COMMIT_GATE_ENABLED", "0").strip().lower() in ("1", "true", "yes")

# Bound on zero-padding written to the far-end recording to keep it wall-clock
# continuous (see the recorder in send_plivo_audio). 60 s at 8 kHz.
FAREND_MAX_PAD_SAMPLES = 60 * PLIVO_SAMPLE_RATE

# Types of aligned loop recordings used for diagnostics (raw near, far reference, and AEC output).
ALIGNED_RECORDING_KINDS = ("nearraw", "farref", "aecout")

# AEC output guard: Prevents the echo canceller from injecting echo by passing 
# raw input instead if the AEC output is louder than the input. Default ON.
AEC_OUTPUT_GUARD_ENABLED = os.getenv("AEC_OUTPUT_GUARD_ENABLED", "1").strip().lower() in ("1", "true", "yes")

# --- Audio-pipeline redesign: Corroborated barge-in gate ---
# Checks if detected speech is just the assistant's own echo by comparing it to the playback.
# If it correlates highly, playback is not truncated.
ECHO_CORR_THRESHOLD = float(os.getenv("ECHO_CORR_THRESHOLD", "0.88"))
ECHO_CORR_WINDOW_MS = 180        # near/far window correlated at a barge-in onset
ECHO_LAG_SEARCH_MS = 80          # +/- search around lag 0 (measured best-lag 50-60 ms)
FAR_END_ACTIVE_FLOOR_DB = -60.0  # below this the far-end is "silent" => never echo
# Far shadow depth: enough history for the window + lag search, with margin.
# ~800 ms at 8 kHz / 20 ms frames.
FAR_SHADOW_FRAMES = 40
# Near-end correlation window in bytes of 16 kHz PCM16 (the preroll's rate).
ECHO_NEAR_BYTES_16K = int(round(ECHO_CORR_WINDOW_MS / 1000.0 * GEMINI_INPUT_RATE)) * 2
# Determines the time window used to search for acoustic echo delays.
ECHO_FAR_MS = ECHO_CORR_WINDOW_MS + ECHO_LAG_SEARCH_MS
ECHO_FAR_BYTES_8K = int(round(ECHO_FAR_MS / 1000.0 * PLIVO_SAMPLE_RATE)) * 2
ECHO_FAR_FRAMES = -(-ECHO_FAR_MS // 20)  # ceil
# Duration the gate continues to check for echo after playback stops, 
# accounting for delayed acoustic echo.
ECHO_TAIL_FRAMES = -(-(2 * ECHO_LAG_SEARCH_MS) // 20)  # ceil
# One 20 ms far-end frame of silence: ulaw_to_pcm of a 160-byte frame yields 160
# PCM16 samples = 320 bytes. Fed to the AEC when nothing is queued for playout.
_FAR_SILENCE_FRAME = b"\x00" * (PLIVO_ULAW_CHUNK_SIZE * 2)
# Master switch for the corroborated gate. Default ON: this IS the fix. Kept as a
# kill-switch so a live call can fall back to the raw gate without a code change
# (ECHO_GATE_ENABLED=0) if tuning ever misbehaves.
ECHO_GATE_ENABLED = os.getenv("ECHO_GATE_ENABLED", "1").strip().lower() in ("1", "true", "yes")
# Echo latch. After an echo verdict, VAD re-fires every few frames for the rest of
# Echo threshold and latch configurations:
# ECHO_LATCH_BREAK_DB: Minimum dB difference required to break an echo verdict (e.g. caller talking over playback).
ECHO_LATCH_BREAK_DB = float(os.getenv("ECHO_LATCH_BREAK_DB", "10"))
# ECHO_MAX_RETURN_DB: Near-end audio within this dB of far-end is never classed as echo.
ECHO_MAX_RETURN_DB = float(os.getenv("ECHO_MAX_RETURN_DB", "-6"))
# Commit gate limits: Avoids stalling indefinitely if playback fails.
GRAPHEME_COMMIT_BYTES = GRAPHEME_COMMIT_MS * PLIVO_SAMPLE_RATE // 1000   # 1920
COMMIT_GATE_IDLE_RELEASE_POLLS = 3

# Max delta traces per turn to prevent unbounded growth on long calls.
DELTA_TRACE_MAX_ENTRIES = 240

VAD_SPEECH_ONSET_FRAMES = 6   # ~120 ms of sustained speech to open a turn (TEST8: 4-frame blips)
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

PROMPT_FILE = os.getenv("PROMPT_FILE", "PROMPT_FILES/prompt_au_v2.txt")  # PROMPT_FILES/prompt_au.txt = rollback
with open(PROMPT_FILE, "r", encoding="utf-8") as f:
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
    parameters={
        "type": "OBJECT",
        "properties": {
            "summary_of_whole_call": {
                "type": "STRING",
                "description": "A concise English summary of the complete call and its outcome."
            }
        },
        "required": ["summary_of_whole_call"]
    }
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

# Course catalog lookups (course_catalog.py, docs/spec/course-catalog-tools-design.md).
catalog_tools = [types.FunctionDeclaration(**spec) for spec in course_catalog.TOOL_SPECS]

LOCAL_GEMINI_TOOLS = [{"function_declarations": [end_call_tool, transfer_call_tool, *catalog_tools]}]
    
SILENCE_FOLLOWUP_PROMPT = (
    "The user has been silent for a few seconds after you spoke with them last."
    "Please briefly and politely re-engage them in your existing conversational style. "
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


# =============================================================================
# Grapheme-cluster helpers  (tasks 5.1 and 5.10)
# =============================================================================
# Grapheme-cluster helpers to correctly split text characters.
# These remain as independent helper functions.

_GRAPHEME_CLUSTER_RE = regex.compile(r"\X")
_BENGALI_VIRAMA = "\u09CD"
# Categories that continue an extended grapheme cluster rather than starting one.
_CLUSTER_EXTENDING_CATEGORIES = ("Mn", "Mc", "Me")


def grapheme_clusters(text):
    """UAX #29 extended grapheme clusters, e.g. ``দুঃখিত -> ['দুঃ', 'খি', 'ত']``."""
    if not text:
        return []
    return _GRAPHEME_CLUSTER_RE.findall(text)


def leading_clusters(text, count=4):
    """The first ``count`` grapheme clusters, for the anomaly log and the gate."""
    return grapheme_clusters(text)[:count]


def ends_mid_grapheme_cluster(text):
    """True when ``text`` stops at a point where its last cluster is unfinished.

    Forward-looking half of the mid-cluster determination: a trailing virama
    (hasant) means a conjunct is still waiting for its second consonant, and a
    trailing combining mark means the cluster is still accreting. The exact,
    retrospective half is ``boundary_splits_grapheme_cluster`` below, which the
    delta trace applies when the *next* delta arrives.
    """
    if not text:
        return False
    last = text[-1]
    if last == _BENGALI_VIRAMA:
        return True
    return unicodedata.category(last) in _CLUSTER_EXTENDING_CATEGORIES


def boundary_splits_grapheme_cluster(prefix, delta):
    """True when the join between ``prefix`` and ``delta`` falls inside a cluster.

    Exact rather than heuristic: if segmenting the concatenation yields fewer
    clusters than segmenting the two pieces separately, the boundary was inside a
    cluster, i.e. ``prefix`` ended mid-cluster and ``delta`` continued it.
    """
    if not prefix or not delta:
        return False
    return (len(grapheme_clusters(prefix + delta))
            < len(grapheme_clusters(prefix)) + len(grapheme_clusters(delta)))


def shared_leading_cluster_count(previous_text, new_text):
    """How many leading grapheme clusters the two transcripts have in common."""
    if not previous_text or not new_text:
        return 0
    shared = 0
    for previous, new in zip(grapheme_clusters(previous_text), grapheme_clusters(new_text)):
        if previous != new:
            break
        shared += 1
    return shared


# =============================================================================
# Per-delta trace  (task 5.1 -- concern (d), observability only)
# =============================================================================
# Logs the exact audio delivered by the model to diagnose repeated audio issues.
# This does not modify the data, it only records it for debugging.

def _short_hash(payload):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def write_aligned_frames(call_state, near_raw, far_ref, aec_out, ref_queue_depth):
    """Persist one inbound block to the three aligned recordings.

    Observation only: the buffers are the ones the inbound loop already holds,
    and nothing is returned. ``aec_out`` is normally the same length as
    ``near_raw`` (one 160-sample AEC block per Plivo frame); if the canceller
    ever buffers, the anchor line's ``aec_samples`` exposes the drift. Every
    write is wrapped so a debug recorder can never break a live call.
    """
    writers = call_state.get("debug_aligned_wav_writers")
    if not writers:
        return
    for kind, pcm in (("nearraw", near_raw), ("farref", far_ref), ("aecout", aec_out)):
        writer = writers.get(kind)
        if writer:
            try:
                writer.writeframes(pcm)
            except Exception:
                pass
    call_state["aligned_frames_written"] += 1
    call_state["aligned_near_samples_written"] += len(near_raw) // 2
    call_state["aligned_aec_samples_written"] += len(aec_out) // 2
    frame = call_state["aligned_frames_written"]
    if frame % LOG_EVERY_N_CHUNKS == 1:
        logger.info(
            f"🎛️ [ALIGNED-REC] frame={frame} t_mono={time.monotonic():.6f} "
            f"near_samples={call_state['aligned_near_samples_written']} "
            f"aec_samples={call_state['aligned_aec_samples_written']} "
            f"ref_queue_depth={ref_queue_depth} "
            f"far_silent_frames={call_state['far_silent_frames']} "
            f"aec_guard_fallbacks={call_state['aec_guard_fallback_frames']}"
        )


def guard_aec_output(call_state, near_raw, aec_out):
    """Return the frame to pass downstream: ``aec_out``, or ``near_raw`` if louder.

    Only whole frames the canceller returned at the input's length are judged;
    anything else (empty, buffered) passes through as the canceller returned it.
    Silent in-call (the pinned log sequences stay as they are): the counters are
    reported on the [ALIGNED-REC] anchors and in the call stats.
    """
    if not AEC_OUTPUT_GUARD_ENABLED or not aec_out or len(aec_out) != len(near_raw):
        return aec_out
    call_state["aec_guard_frames_checked"] += 1
    if audioop.rms(aec_out, 2) <= audioop.rms(near_raw, 2):
        return aec_out
    call_state["aec_guard_fallback_frames"] += 1
    return near_raw


def reset_far_reference(call_state):
    """Reset the AEC far-end reference and the playout-paced feed together.

    ``aec.reset_far_end()`` clears the canceller's far FIFO (learned filter
    weights survive — the acoustic path hasn't changed). The redesign's
    playout-paced feed adds two more pieces of state that track the same
    reference and MUST clear in lockstep, or the reset leaks:

    * ``farend_ref_queue`` — frames sent but not yet consumed by the inbound
      loop. After a ``clearAudio`` these frames were discarded by Plivo and will
      never be heard, so feeding them to the AEC would re-introduce exactly the
      send-vs-playout desync this redesign removes.
    * ``far_shadow`` — the aligned far history the barge-in gate correlates
      against. Stale entries would make the gate compare near-end against audio
      the caller never heard.

    Also drops the echo latch: it was set against the audio just discarded.

    Used at every point that sends ``clearAudio`` and on Gemini reconnect.
    """
    call_state["aec"].reset_far_end()
    call_state["farend_ref_queue"].clear()
    call_state["far_shadow"].clear()
    call_state["far_silent_frames"] = ECHO_TAIL_FRAMES
    close_echo_latch_episode(call_state, "reset")


def far_window_has_playback(call_state):
    """True while echo of real playback can still be arriving.

    Counts inbound frames since the last real far frame was consumed. The echo
    of that frame can arrive up to 2 * ECHO_LAG_SEARCH_MS later (the longest
    delay the gate searches), so the gate keeps running for ECHO_TAIL_FRAMES
    after playback stops, whatever ``assistant_speaking`` says.
    """
    return call_state["far_silent_frames"] < ECHO_TAIL_FRAMES


def evaluate_echo_gate(call_state):
    """Corroborate a while-speaking VAD onset against the far-end (concerns a+b).

    Returns a ``bargein.BargeInDecision``. The near-end window is the tail of the
    preroll (post-AEC/RNNoise/AGC 16 kHz — the same signal that measured 0.97
    envelope correlation against the far-end on the reproduction call). The
    far-end window is the tail of ``far_shadow``, which is built frame-for-frame
    with the preroll in the inbound loop, so the two are already aligned and only
    the small acoustic/buffering lag (~50-60 ms measured) needs searching. Both
    windows end now; the far one leads by ECHO_LAG_SEARCH_MS (see ECHO_FAR_MS),
    so the decision's ``lag_bins`` means an echo delay of
    ECHO_LAG_SEARCH_MS + 10 * lag_bins ms.

    An echo verdict whose echo return exceeds ECHO_MAX_RETURN_DB is overturned
    to ``near-too-loud-for-echo``: correlated or not, a near-end that loud is
    not residual echo.

    Pure decision, no side effects: the caller acts on the verdict.
    """
    near = bytes(call_state["preroll_pcm16"][-ECHO_NEAR_BYTES_16K:])
    shadow = call_state["far_shadow"]
    far = b"".join(list(shadow)[-ECHO_FAR_FRAMES:])[-ECHO_FAR_BYTES_8K:] if shadow else b""
    decision = bargein.should_barge_in(
        near, far,
        near_rate=GEMINI_INPUT_RATE, far_rate=PLIVO_SAMPLE_RATE,
        threshold=ECHO_CORR_THRESHOLD,
        lag_search_ms=ECHO_LAG_SEARCH_MS,
        floor_dbfs=FAR_END_ACTIVE_FLOOR_DB,
    )
    if decision.is_echo and echo_return_db(call_state, decision) > ECHO_MAX_RETURN_DB:
        return bargein.BargeInDecision(
            barge_in=True, is_echo=False, reason="near-too-loud-for-echo",
            correlation=decision.correlation, lag_bins=decision.lag_bins,
            far_active=decision.far_active, near_dbfs=decision.near_dbfs,
            far_dbfs=decision.far_dbfs,
        )
    return decision


def echo_return_db(call_state, decision):
    """Near-end level before AGC, relative to the far-end, in dB.

    ``decision.near_dbfs`` is measured on the post-AGC preroll, and AGC pulls
    everything toward AGC_TARGET_DB, so the raw near level cannot tell a louder
    caller from AGC catching up on echo. Removing the current AGC gain recovers
    the level the canceller output actually had; against the far level that is
    the echo return, which stays roughly steady for echo.
    """
    gain_db = 20.0 * float(np.log10(call_state["agc_current_gain_lin"] + 1e-12))
    return decision.near_dbfs - gain_db - decision.far_dbfs


def apply_echo_latch(call_state, decision):
    """Combine one gate decision with the playback-period echo latch.

    Returns ``(suppress, reason)``. An echo verdict suppresses and sets (or
    lowers) the latched echo return. A non-echo verdict is still suppressed as
    ``echo-latched`` while a latch is held, the far-end is active, the echo
    return is within ECHO_MAX_RETURN_DB and has not risen more than
    ECHO_LATCH_BREAK_DB above the latched value; otherwise the latch is
    dropped and the decision's own reason stands. The latch is cleared when a
    new playback period starts and by ``reset_far_reference``.
    """
    erl_db = echo_return_db(call_state, decision)
    latched = call_state.get("echo_latch_erl_db")
    if decision.is_echo:
        call_state["echo_latch_erl_db"] = erl_db if latched is None else min(latched, erl_db)
        return True, decision.reason
    if (latched is not None and decision.far_active
            and erl_db <= ECHO_MAX_RETURN_DB
            and erl_db <= latched + ECHO_LATCH_BREAK_DB):
        return True, "echo-latched"
    call_state["echo_latch_erl_db"] = None
    return False, decision.reason


def _since_playback_ms(call_state):
    start = call_state.get("ai_playback_start_time")
    if start is None:
        return None
    return int((datetime.now(ist_tz) - start).total_seconds() * 1000)


def note_echo_latch_hold(call_state, decision, latched_db):
    """Log an onset suppressed ONLY by the latch.

    The fresh gate verdict for this onset was not-echo, so without the latch it
    would have barged in. Echo re-onsets and a genuine caller at echo level
    (the known limitation, TestCaseDQuietCallerKnownLimitation) both land here
    and cannot be told apart online; ``rise_db`` (echo return above the latched
    value) is the field to histogram offline. Queryable via ``[LATCH-HOLD]``.
    """
    erl_db = echo_return_db(call_state, decision)
    rise_db = erl_db - latched_db
    episode = call_state.get("latch_episode")
    if episode is None:
        episode = {"holds": 0, "first_hold_t": time.monotonic(),
                   "max_rise_db": rise_db, "max_erl_db": erl_db}
        call_state["latch_episode"] = episode
    episode["holds"] += 1
    episode["max_rise_db"] = max(episode["max_rise_db"], rise_db)
    episode["max_erl_db"] = max(episode["max_erl_db"], erl_db)
    stats = call_state.setdefault("latch_stats", new_latch_stats())
    stats["holds"] += 1
    stats["max_rise_db"] = rise_db if stats["max_rise_db"] is None else max(stats["max_rise_db"], rise_db)
    logger.info(
        f"🔒 [LATCH-HOLD] call={call_state.get('call_uuid')} hold={episode['holds']} "
        f"since_play_ms={_since_playback_ms(call_state)} fresh_reason={decision.reason} "
        f"corr={decision.correlation:.3f} erl_db={erl_db:.1f} latched_db={latched_db:.1f} "
        f"rise_db={rise_db:.1f} break_at_db={latched_db + ECHO_LATCH_BREAK_DB:.1f} "
        f"ceiling_db={ECHO_MAX_RETURN_DB:.1f} near_dbfs={decision.near_dbfs:.1f} "
        f"far_dbfs={decision.far_dbfs:.1f}"
    )


def new_latch_stats():
    return {"holds": 0, "episodes_broken": 0, "episodes_unbroken": 0, "max_rise_db": None}


def close_echo_latch_episode(call_state, ended_by, erl_db=None):
    """Drop the latch and, if it held or was broken, log how the episode ended.

    ``ended_by`` is ``break-rise`` / ``break-ceiling`` / ``break-far-inactive``
    (the caller got through), or ``reset`` / ``new-playback`` / ``call-end``
    (the latch was dropped without the caller ever getting through). An
    unbroken episode with holds > 0 is the failure mode to count: every hold in
    it may have been a caller who was never heard. Echo-only episodes (no
    holds, never broken) are not logged. Queryable via ``[LATCH-END]``.
    """
    episode = call_state.get("latch_episode")
    broken = ended_by.startswith("break")
    call_state["echo_latch_erl_db"] = None
    call_state["latch_episode"] = None
    if episode is None and not broken:
        return
    holds = episode["holds"] if episode else 0
    stats = call_state.setdefault("latch_stats", new_latch_stats())
    stats["episodes_broken" if broken else "episodes_unbroken"] += 1
    held_ms = int((time.monotonic() - episode["first_hold_t"]) * 1000) if episode else 0
    max_rise = f"{episode['max_rise_db']:.1f}" if episode else "none"
    max_erl = f"{episode['max_erl_db']:.1f}" if episode else "none"
    erl_txt = "none" if erl_db is None else f"{erl_db:.1f}"
    logger.info(
        f"🔓 [LATCH-END] call={call_state.get('call_uuid')} ended_by={ended_by} "
        f"holds={holds} held_ms={held_ms} max_rise_db={max_rise} max_erl_db={max_erl} "
        f"erl_db={erl_txt} since_play_ms={_since_playback_ms(call_state)}"
    )


def reset_delta_trace(call_state, reason=""):
    """Start a fresh per-turn trace. Called at every turn boundary."""
    call_state["delta_trace"] = []
    call_state["delta_trace_dropped"] = 0
    call_state["delta_seq"] = 0
    call_state["model_audio_bytes_received"] = 0
    call_state["model_audio_chunk_hashes"] = []
    call_state["model_text_deltas"] = []
    call_state["queued_bytes"] = 0
    call_state["sent_bytes"] = 0
    call_state["sent_chunk_hashes"] = set()
    call_state["duplicate_sent_chunk_hashes"] = 0
    if reason:
        logger.debug(f"🧾 [DELTA] trace reset ({reason})")


def _append_delta(call_state, entry):
    trace = call_state["delta_trace"]
    trace.append(entry)
    if len(trace) > DELTA_TRACE_MAX_ENTRIES:
        del trace[0]
        call_state["delta_trace_dropped"] += 1


def record_audio_delta(call_state, model_bytes, queued):
    """One ``inline_data`` part. ``queued`` is False when app.py drops it locally."""
    call_state["delta_seq"] += 1
    digest = _short_hash(model_bytes)
    call_state["model_audio_bytes_received"] += len(model_bytes)
    call_state["model_audio_chunk_hashes"].append(digest)
    entry = {
        "seq": call_state["delta_seq"],
        "kind": "audio",
        "t_mono": round(time.monotonic(), 6),
        "bytes": len(model_bytes),
        "cumulative_bytes": call_state["model_audio_bytes_received"],
        "hash": digest,
        "queued": queued,
    }
    _append_delta(call_state, entry)
    logger.debug(
        f"🧾 [DELTA] audio seq={entry['seq']} t_mono={entry['t_mono']} "
        f"bytes={entry['bytes']} cum={entry['cumulative_bytes']} hash={digest} "
        f"queued={queued}"
    )
    return entry


def record_text_delta(call_state, prefix, delta_text):
    """One ``output_transcription`` delta, with its cluster-boundary verdict."""
    call_state["delta_seq"] += 1
    joined_mid_cluster = boundary_splits_grapheme_cluster(prefix, delta_text)
    if joined_mid_cluster:
        for earlier in reversed(call_state["delta_trace"]):
            if earlier["kind"] == "text":
                earlier["ends_mid_cluster"] = True
                break
    encoded = delta_text.encode("utf-8")
    entry = {
        "seq": call_state["delta_seq"],
        "kind": "text",
        "t_mono": round(time.monotonic(), 6),
        "bytes": len(encoded),
        "cumulative_bytes": len((prefix + delta_text).encode("utf-8")),
        "hash": _short_hash(encoded),
        "text": delta_text,
        "clusters": grapheme_clusters(delta_text),
        "ends_mid_cluster": ends_mid_grapheme_cluster(delta_text),
        "continues_previous_cluster": joined_mid_cluster,
    }
    call_state["model_text_deltas"].append(delta_text)
    _append_delta(call_state, entry)
    logger.debug(
        f"🧾 [DELTA] text seq={entry['seq']} t_mono={entry['t_mono']} "
        f"bytes={entry['bytes']} cum={entry['cumulative_bytes']} hash={entry['hash']} "
        f"ends_mid_cluster={entry['ends_mid_cluster']} "
        f"continues_previous_cluster={joined_mid_cluster} text={delta_text!r}"
    )
    return entry


def delta_trace_verdict(call_state):
    """Derive the three mutually exclusive verdicts as fields.

    DIMENSIONAL NOTE, because the design's phrasing ``sent_bytes >
    model_audio_bytes_received`` is not directly comparable: the model's audio is
    24 kHz PCM16 (2 bytes/sample) while the outbound stream is 8 kHz mu-law
    (1 byte/sample), a factor of exactly 6. The comparison is therefore made
    against ``model_audio_bytes_received // 6``, reported as
    ``model_ulaw_equivalent_bytes``.
    """
    hashes = call_state["model_audio_chunk_hashes"]
    duplicate_model_hashes = len(hashes) - len(set(hashes))
    text_deltas = call_state["model_text_deltas"]
    duplicate_text_deltas = 0
    if len(text_deltas) > 1:
        seen = set()
        for delta in text_deltas:
            if delta.strip() and delta in seen:
                duplicate_text_deltas += 1
            seen.add(delta)
    model_equivalent = call_state["model_audio_bytes_received"] // 6
    sent = call_state["sent_bytes"]
    duplicate_sent = call_state["duplicate_sent_chunk_hashes"]

    if duplicate_model_hashes or duplicate_text_deltas:
        verdict = "upstream-duplication"
    elif sent > model_equivalent or duplicate_sent:
        verdict = "downstream-re-delivery"
    else:
        verdict = "neither"

    return {
        "verdict": verdict,
        "model_audio_bytes_received": call_state["model_audio_bytes_received"],
        "model_ulaw_equivalent_bytes": model_equivalent,
        "queued_bytes": call_state["queued_bytes"],
        "sent_bytes": sent,
        "duplicate_model_chunk_hashes": duplicate_model_hashes,
        "duplicate_model_text_deltas": duplicate_text_deltas,
        "duplicate_sent_chunk_hashes": duplicate_sent,
        "entries": len(call_state["delta_trace"]),
        "entries_dropped": call_state["delta_trace_dropped"],
    }


def dump_delta_trace(call_state, reason):
    """Promote the whole trace to INFO. Called only when an anomaly fires, so the
    evidence survives without running the whole call at DEBUG."""
    summary = delta_trace_verdict(call_state)
    logger.info(f"🧾 [DELTA-TRACE] dump reason={reason} | {summary}")
    for entry in call_state["delta_trace"]:
        logger.info(f"🧾 [DELTA-TRACE]   {entry}")
    return summary


# Commit gate logic: Delays playback of words slightly after an abnormal interruption, 
# ensuring syllables play completely instead of stuttering.
# This remains inactive unless an anomaly is detected.

def arm_commit_gate(call_state, truncated_text, delivered_ms):
    clusters = leading_clusters(truncated_text)
    already_armed = call_state["commit_gate_armed"]
    call_state["commit_gate_armed"] = True
    call_state["commit_gate_armed_text"] = truncated_text
    call_state["commit_gate_armed_clusters"] = clusters
    call_state["commit_gate_last_verdict"] = ""
    call_state["commit_gate_committed"] = False
    logger.info(
        f"🧩 [COMMIT-GATE] {'re-armed' if already_armed else 'armed'} | "
        f"trigger_delivered_ms={delivered_ms:.0f} | "
        f"commit_ms={GRAPHEME_COMMIT_MS} | commit_bytes={GRAPHEME_COMMIT_BYTES} | "
        f"leading_clusters={clusters} | ai_text={truncated_text.strip()!r}"
    )


def disarm_commit_gate(call_state, reason):
    if not call_state["commit_gate_armed"]:
        return
    call_state["commit_gate_armed"] = False
    call_state["commit_gate_armed_text"] = ""
    call_state["commit_gate_armed_clusters"] = []
    call_state["commit_gate_holding"] = False
    call_state["commit_gate_idle_polls"] = 0
    call_state["commit_gate_last_verdict"] = ""
    call_state["commit_gate_committed"] = False
    logger.info(f"🧩 [COMMIT-GATE] disarmed | reason={reason}")


def commit_gate_verdict(call_state):
    """``off`` | ``hold-pending-text`` | ``hold-shared-prefix`` | ``release-no-match``.

    ``hold-pending-text`` exists because the Live API's ``output_transcription``
    deltas are not time-aligned with its ``inline_data`` chunks, so audio can
    arrive before any text does. With no text to compare, "unknown" is treated as
    a match: withholding 240 ms is recoverable, emitting the fragment a second
    time is what requirement 2.2 forbids.
    """
    if not call_state["commit_gate_armed"]:
        return "off", 0
    new_text = call_state["ai_text_buffer"]
    if not new_text.strip():
        return "hold-pending-text", 0
    shared = shared_leading_cluster_count(call_state["commit_gate_armed_text"], new_text)
    if shared > 0:
        return "hold-shared-prefix", shared
    return "release-no-match", 0


def commit_gate_discard(call_state, reason):
    """Drop withheld audio that will never be played — this is the "caller hears
    nothing rather than another partial cluster" half of requirement 2.2."""
    held = len(call_state["commit_gate_hold"])
    # The utterance is over either way, so the next one re-evaluates from scratch.
    call_state["commit_gate_committed"] = False
    call_state["commit_gate_last_verdict"] = ""
    if not held:
        return
    call_state["commit_gate_hold"].clear()
    call_state["commit_gate_holding"] = False
    call_state["commit_gate_idle_polls"] = 0
    logger.info(
        f"🧩 [COMMIT-GATE] withheld audio discarded | bytes={held} | "
        f"ms={held / 8.0:.0f} | reason={reason}"
    )


# Fallback log value used when trigger evidence wasn't recorded (e.g. gate disabled).
FAR_END_EVIDENCE_NOT_MEASURED = "not-measured[concern-a-parked]"


def _format_echo_evidence(decision):
    """Render the corroborated-gate evidence for the [TRIGGER]/[ANOMALY] logs.

    The redesign's gate now measures exactly the fields concern (a) had parked.
    When a decision exists it is rendered as corr/lag/far-active; when the gate
    is disabled or no decision was taken for this trigger, the honest
    not-measured placeholder is kept rather than a fabricated value.
    """
    if decision is None:
        return FAR_END_EVIDENCE_NOT_MEASURED
    return (f"corr={decision.correlation:.3f},lag_ms={decision.lag_bins * 10},delay_ms={ECHO_LAG_SEARCH_MS + decision.lag_bins * 10},"
            f"far_active={decision.far_active},reason={decision.reason}")


app = Quart(__name__)

CALL_HISTORY_DB_PATH = "call_history.db"

def init_call_history_db():
    with sqlite3.connect(CALL_HISTORY_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_uuid TEXT UNIQUE,
                name TEXT,
                phone TEXT,
                status TEXT,
                fail_reason TEXT,
                cost TEXT,
                end_reason TEXT,
                created_at TEXT,
                updated_at TEXT,
                duration_sec INTEGER,
                connected_at TEXT
            )
        """)
        conn.commit()

init_call_history_db()

def persist_call_event_sync(call_uuid, event_type, data):
    if not call_uuid:
        return
    now_ist_str = datetime.now(ist_tz).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with sqlite3.connect(CALL_HISTORY_DB_PATH) as conn:
            data = data or {}
            
            if event_type == "call_queued":
                name = data.get("name", "Unknown")
                phone = data.get("phone", "")
                conn.execute("""
                    INSERT INTO calls (call_uuid, name, phone, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(call_uuid) DO UPDATE SET
                        status=excluded.status, updated_at=excluded.updated_at
                """, (call_uuid, name, phone, "queued", now_ist_str, now_ist_str))
            
            elif event_type == "call_ringing":
                conn.execute("UPDATE calls SET status='ringing', updated_at=? WHERE call_uuid=?", (now_ist_str, call_uuid))
                
            elif event_type == "call_connected":
                conn.execute("UPDATE calls SET status='connected', updated_at=?, connected_at=? WHERE call_uuid=?", 
                             (now_ist_str, now_ist_str, call_uuid))
                             
            elif event_type in ("call_ended", "call_failed"):
                if event_type == "call_ended":
                    status = "ended"
                    cost = data.get("cost")
                    end_reason = data.get("reason")
                    fail_reason = None
                else:
                    status = "failed"
                    cost = None
                    end_reason = None
                    fail_reason = data.get("error")
                
                row = conn.execute("SELECT connected_at FROM calls WHERE call_uuid=?", (call_uuid,)).fetchone()
                duration_sec = None
                if row and row[0]:
                    try:
                        conn_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                        duration_sec = int((datetime.now(ist_tz).replace(tzinfo=None) - conn_time).total_seconds())
                    except Exception as e:
                        logger.error(f"Error parsing connected_at {row[0]}: {e}")
                        
                conn.execute("""
                    UPDATE calls 
                    SET status=?, updated_at=?, cost=?, end_reason=?, fail_reason=?, duration_sec=?
                    WHERE call_uuid=?
                """, (status, now_ist_str, cost, end_reason, fail_reason, duration_sec, call_uuid))
                
            conn.commit()
    except Exception as e:
        logger.error(f"Error persisting call event {event_type} for {call_uuid}: {e}")

# --- SSE (Server-Sent Events) Infrastructure for Dashboard ---
call_event_subscribers = set()

def emit_call_event(call_id, event_type, data=None):
    """Fire-and-forget: push a JSON event to all connected dashboard browsers."""
    global call_event_subscribers
    
    if call_id and event_type in ["call_queued", "call_ringing", "call_connected", "call_ended", "call_failed"]:
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, persist_call_event_sync, call_id, event_type, data)
        except RuntimeError:
            pass  # Fallback if no loop

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

@app.route('/api/call-history')
async def get_call_history():
    args = dict(request.args)
    
    def fetch_history():
        start_date = args.get('start_date')
        end_date = args.get('end_date')
        start_hour = args.get('start_hour')
        end_hour = args.get('end_hour')
        name_filter = args.get('name')
        status_filter = args.get('status')
        limit = min(int(args.get('limit', 50)), 200)
        offset = int(args.get('offset', 0))
        
        query = "SELECT call_uuid, name, phone, status, fail_reason, cost, end_reason, created_at, duration_sec FROM calls WHERE 1=1"
        params = []
        
        if start_date:
            query += " AND date(created_at) >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date(created_at) <= ?"
            params.append(end_date)
        if start_hour is not None and start_hour != "":
            query += " AND cast(strftime('%H', created_at) as integer) >= ?"
            params.append(int(start_hour))
        if end_hour is not None and end_hour != "":
            query += " AND cast(strftime('%H', created_at) as integer) <= ?"
            params.append(int(end_hour))
        if name_filter:
            query += " AND name LIKE ?"
            params.append(f"%{name_filter}%")
        if status_filter and status_filter.lower() != "all":
            query += " AND status = ?"
            params.append(status_filter.lower())
            
        query += " ORDER BY datetime(created_at) DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        try:
            with sqlite3.connect(CALL_HISTORY_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query, params).fetchall()
                
                count_query = query.replace("SELECT call_uuid, name, phone, status, fail_reason, cost, end_reason, created_at, duration_sec", "SELECT count(*)", 1)
                count_query = count_query.split(" ORDER BY")[0]
                total = conn.execute(count_query, params[:-2]).fetchone()[0]
                
            calls = []
            for r in rows:
                d = dict(r)
                if d["created_at"]:
                    d["created_at"] = d["created_at"].replace(" ", "T") + "+05:30"
                calls.append(d)
                
            return {
                "calls": calls,
                "total": total,
                "has_more": offset + limit < total
            }
        except Exception as e:
            logger.error(f"Error fetching call history: {e}")
            return {"calls": [], "total": 0, "has_more": False}
    
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, fetch_history)
    return result

plivo_client = plivo.RestClient(auth_id=os.getenv('PLIVO_AUTH_ID'), auth_token=os.getenv('PLIVO_AUTH_TOKEN'))

@app.route('/playback_audio_files/<path:filename>')
async def serve_audio(filename):
    """Serves audio files from the local audio_files directory."""
    logger.info(f"Serving audio file: {filename}")
    return await send_from_directory('playback_audio_files', filename)

@app.route('/atc.png', methods=['GET'])
async def serve_atc_logo():
    return await send_file('atc.png')

@app.route('/AU_Logo.png', methods=['GET'])
async def serve_au_logo():
    res = await send_file('AU_Logo.png')
    res.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return res

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
        <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" contentType="audio/x-mulaw;rate=8000" audioTrack="inbound" >
            {ws_base_url}/media-stream?user_name={encoded_name}&amp;phone_number={encoded_phone}
        </Stream>
    </Response>
    '''
    
    #noiseCancellation="true" noiseCancellationLevel="85"
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
                contentType="audio/x-mulaw;rate=8000" audioTrack="inbound">
            wss://{ws_host}/media-stream?phone_number={encoded_phone}&amp;transfer_context_id={encoded_context_id}&amp;dial_status={encoded_status}&amp;is_transfer_return=1
        </Stream>
    </Response>
    """

    #noiseCancellation="true" noiseCancellationLevel="85" 
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


_vertex_credentials = None
_vertex_token_lock = asyncio.Lock()


def vertex_base_url(location):
    """google-genai 1.66 builds https://{location}-aiplatform.googleapis.com/,
    which is right for a region such as us-central1 but not for the us / eu
    multi-regions, whose hostnames are aiplatform.{us,eu}.rep.googleapis.com.
    Returns the override, or None when the SDK default is correct."""
    if location in ("us", "eu"):
        return f"https://aiplatform.{location}.rep.googleapis.com/"
    return None


def load_vertex_credentials():
    """Service-account credentials, loaded once per process and shared by every
    call, so the access token is cached across calls rather than re-minted."""
    global _vertex_credentials
    if _vertex_credentials is None:
        from google.oauth2 import service_account
        _vertex_credentials = service_account.Credentials.from_service_account_file(
            VERTEX_CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    return _vertex_credentials


def vertex_token_needs_refresh(credentials, now=None):
    if not credentials.token or not credentials.valid or credentials.expiry is None:
        return True
    # google-auth keeps expiry as a naive UTC datetime.
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return (credentials.expiry - now).total_seconds() < VERTEX_TOKEN_REFRESH_MARGIN_SECONDS


async def ensure_vertex_token():
    """The SDK refreshes an expired token synchronously inside live.connect(),
    which would block the event loop (every call's audio) for an HTTP round trip
    that GEMINI_CONNECT_TIMEOUT_SECONDS cannot cut short. Refresh here instead,
    in a worker thread and ahead of expiry, so the SDK always finds it valid."""
    credentials = load_vertex_credentials()
    async with _vertex_token_lock:
        if vertex_token_needs_refresh(credentials):
            import google.auth.transport.requests
            await asyncio.to_thread(
                credentials.refresh, google.auth.transport.requests.Request()
            )
            logger.info("🔐 Vertex AI access token refreshed")


def create_gemini_client():
    if GEMINI_BACKEND == "studio":
        return genai.Client(api_key=LIVE_API_KEY)
    base_url = vertex_base_url(VERTEX_LOCATION)
    return genai.Client(
        vertexai=True,
        project=VERTEX_PROJECT,
        location=VERTEX_LOCATION,
        credentials=load_vertex_credentials(),
        http_options=types.HttpOptions(base_url=base_url) if base_url else None,
    )


def describe_gemini_backend():
    if GEMINI_BACKEND == "studio":
        return "backend=studio"
    return f"backend=vertex project={VERTEX_PROJECT} location={VERTEX_LOCATION}"


@contextlib.asynccontextmanager
async def connect_live_with_timeout(client, model, config):
    if GEMINI_BACKEND == "vertex":
        await asyncio.wait_for(ensure_vertex_token(), timeout=GEMINI_CONNECT_TIMEOUT_SECONDS)
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
    try:
        done, pending = await asyncio.wait(
            task_map.values(),
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        for task in task_map.values():
            task.cancel()
        await asyncio.gather(*task_map.values(), return_exceptions=True)
        raise
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
    # Task 5.2 / defect 1.8: without this the next affected call is as
    # unattributable from its own log as the reported one was.
    logger.info(
        f"🧬 [MODEL] {GEMINI_MODEL} | gemini_reconnect_count: "
        f"{call_state.get('gemini_reconnect_count', 0)}"
    )
    close_echo_latch_episode(call_state, "call-end")
    latch = call_state.get("latch_stats") or new_latch_stats()
    max_rise = "none" if latch["max_rise_db"] is None else f"{latch['max_rise_db']:.1f}"
    logger.info(
        f"🔒 [LATCH] holds={latch['holds']} episodes_broken={latch['episodes_broken']} "
        f"episodes_unbroken={latch['episodes_unbroken']} max_rise_db={max_rise}"
    )
    checked = call_state.get("aec_guard_frames_checked", 0)
    fallbacks = call_state.get("aec_guard_fallback_frames", 0)
    share = f"{100.0 * fallbacks / checked:.1f}%" if checked else "n/a"
    logger.info(
        f"🛡️ [AEC-GUARD] enabled={AEC_OUTPUT_GUARD_ENABLED} fallback_frames={fallbacks} "
        f"checked={checked} share={share}"
    )
    # aec1 exposes a stats dict; aec.py has none, so only the impl is logged.
    aec_stats = getattr(call_state.get("aec"), "stats", None)
    aec_detail = "".join(f" {k}={v}" for k, v in aec_stats.items()) if aec_stats else ""
    logger.info(f"🎛️ [AEC] impl={AEC_IMPL}{aec_detail}")
    logger.info(f"🔎 [CATALOG] calls={call_state.get('catalog_calls', 0)} chars={call_state.get('catalog_chars', 0)}")
    logger.info(f"Tokens Used - Text In: {call_state['tokens_text_in']}, Audio In: {call_state['tokens_audio_in']}, Text Out: {call_state['tokens_text_out']}, Audio Out: {call_state['tokens_audio_out']}")
    logger.info(f"Last usage_metadata.total_token_count seen: {call_state['last_usage_total_token_count']}")
    logger.info(f"usage_metadata.total_token_count updates: {call_state['usage_total_updates']}, non-monotonic transitions: {call_state['usage_total_non_monotonic_count']}")
    logger.info(f"Estimated Call Cost (lower-bound): ${total_cost:.6f}")
    logger.info(f"⚠️  Note: Actual cost is higher due to compounding — past tokens are re-billed each turn.")
    logger.info("====================================")
    reason = call_state.get("end_call_summary", "")
    if not reason:
        reason = "call completed"
        
    emit_call_event(call_state.get("call_uuid"), "call_ended", {
        "reason": reason,
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
    user_language = "English"

    if is_returning_from_transfer and transfer_summary_raw:
        try:
            parsed_summary = json.loads(transfer_summary_raw)
            call_summary = parsed_summary.get("call_summary", "")
            user_language = parsed_summary.get("language", "English")
        except json.JSONDecodeError:
            logger.warning("Could not parse transfer summary as JSON. Falling back to raw string.")
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

        # Redesign Part 1: playout-paced far-end reference.
        # emit_chunk (send task) appends each sent far frame here; the inbound
        # loop pops one per processed block, so the AEC reference and far_shadow
        # advance at realtime (playout) rate rather than in the send burst. This
        # keeps the AEC reference aligned to playout AND keeps far_shadow aligned
        # frame-for-frame with the near-end preroll, so the barge-in correlation
        # gate needs no cross-timebase alignment.
        "farend_ref_queue": collections.deque(),
        "far_shadow": collections.deque(maxlen=FAR_SHADOW_FRAMES),
        # Echo return (dB) latched by the barge-in gate's last echo verdict in
        # this playback period, or None. See apply_echo_latch.
        "far_silent_frames": ECHO_TAIL_FRAMES,
        "echo_latch_erl_db": None,
        "latch_episode": None,
        "latch_stats": new_latch_stats(),
        
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
        
        # Debug audio recording
        "debug_wav_writer": None,

        # Far-end (outbound) debug recording — task 5.4a. 8 kHz, pre-resample
        # telephony audio, paired with the 16 kHz inbound writer above so the next
        # occurrence has BOTH sides on disk.
        "debug_farend_wav_writer": None,
        "farend_samples_written": 0,
        "farend_chunks_written": 0,
        "farend_t0_mono": None,
        "farend_pad_samples": 0,

        # Aligned inbound-loop recordings (TEST1 follow-up). Three 8 kHz files
        # written one frame per processed inbound block, so they line up
        # sample-for-sample by construction: raw near-end (pre-AEC), the
        # playout-paced far reference the AEC and gate actually used, and the
        # AEC output (pre-RNNoise). Observation only.
        "debug_aligned_wav_writers": {},
        "aligned_frames_written": 0,
        "aligned_near_samples_written": 0,
        "aligned_aec_samples_written": 0,

        # AEC output guard (TEST2 follow-up): frames judged, and frames where
        # the AEC output was louder than its input so the raw input was used.
        "aec_guard_frames_checked": 0,
        "aec_guard_fallback_frames": 0,

        # Course catalog tool calls this call, and the response characters sent.
        "catalog_calls": 0,
        "catalog_chars": 0,

        # Per-delta trace and byte counters — task 5.1. Observability only.
        "delta_trace": [],
        "delta_trace_dropped": 0,
        "delta_seq": 0,
        "model_audio_bytes_received": 0,
        "model_audio_chunk_hashes": [],
        "model_text_deltas": [],
        "queued_bytes": 0,
        "sent_bytes": 0,
        "sent_chunk_hashes": set(),
        "duplicate_sent_chunk_hashes": 0,

        # Leading-fragment commit gate — task 5.10. Inert until an anomalous
        # truncation arms it; a healthy call never leaves the "off" state.
        "commit_gate_armed": False,
        "commit_gate_armed_text": "",
        "commit_gate_armed_clusters": [],
        "commit_gate_hold": bytearray(),
        "commit_gate_holding": False,
        "commit_gate_idle_polls": 0,
        "commit_gate_last_verdict": "",
        "commit_gate_committed": False,
    }
    
    disclaimer_finished = asyncio.Event()
    disclaimer_task = None

    try:
        await wait_for_plivo_start(plivo_ws, call_state)

        # Open debug WAV recorder for preprocessed audio
        try:
            os.makedirs("debug_recordings", exist_ok=True)
            safe_name = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in user_name)
            timestamp_str = datetime.now(ist_tz).strftime("%Y%m%d_%H%M%S")
            debug_wav_path = f"debug_recordings/{safe_name}_{timestamp_str}.wav"
            debug_wf = wave.open(debug_wav_path, "wb")
            debug_wf.setnchannels(1)
            debug_wf.setsampwidth(2)  # 16-bit
            debug_wf.setframerate(GEMINI_INPUT_RATE)  # 16kHz
            call_state["debug_wav_writer"] = debug_wf
            logger.info(f"🎙️ Debug recording started: {debug_wav_path}")
        except Exception as e:
            logger.warning(f"⚠️ Could not open debug WAV file: {e}")

        # Task 5.4a — the outbound far-end, written next to the inbound recording
        # so cross-correlating the two sides is possible for the first time. The
        # rates DIFFER on purpose: the inbound writer above records post-resample
        # ``clean_pcm_16k`` at 16 kHz, while the far-end is tapped pre-resample at
        # the telephony rate. Declaring 16 kHz here would silently corrupt every
        # future lag estimate. Opened in its own try/except so a failure here
        # cannot take the inbound recording down with it.
        try:
            # Same ``safe_name`` / ``timestamp_str`` as the inbound writer, so the
            # pair sorts together and the pairing needs no log parsing.
            farend_wav_path = f"debug_recordings/{safe_name}_{timestamp_str}_farend.wav"
            farend_wf = wave.open(farend_wav_path, "wb")
            farend_wf.setnchannels(1)
            farend_wf.setsampwidth(2)  # 16-bit
            farend_wf.setframerate(PLIVO_SAMPLE_RATE)  # 8 kHz — NOT GEMINI_INPUT_RATE
            call_state["debug_farend_wav_writer"] = farend_wf
            logger.info(
                f"🎚️ [FAR-END] recording started: {farend_wav_path} "
                f"(mono/16-bit/{PLIVO_SAMPLE_RATE} Hz; pairs with {debug_wav_path} "
                f"at {GEMINI_INPUT_RATE} Hz)"
            )
        except Exception as e:
            logger.warning(f"⚠️ Could not open far-end debug WAV file: {e}")

        # Aligned inbound-loop recordings. The pair above cannot be globally
        # aligned (send-time far-end vs a near-end that catches up a backlog
        # after the disclaimer), which left the TEST1 echo delay unmeasurable.
        # These three are written in the same loop iteration, so frame N of each
        # is the same 20 ms. Each opened on its own so one failure costs one file.
        for kind in ALIGNED_RECORDING_KINDS:
            try:
                aligned_path = f"debug_recordings/{safe_name}_{timestamp_str}_{kind}.wav"
                aligned_wf = wave.open(aligned_path, "wb")
                aligned_wf.setnchannels(1)
                aligned_wf.setsampwidth(2)  # 16-bit
                aligned_wf.setframerate(PLIVO_SAMPLE_RATE)  # 8 kHz
                call_state["debug_aligned_wav_writers"][kind] = aligned_wf
                logger.info(f"🎛️ [ALIGNED-REC] recording started: {aligned_path}")
            except Exception as e:
                logger.warning(f"⚠️ Could not open aligned debug WAV '{kind}': {e}")

        disclaimer_task = asyncio.create_task(
            play_disclaimer(plivo_ws, call_state, disclaimer_finished)
        )

        client = create_gemini_client()

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
                System Event: The user was previously speaking with you, was then transferred to a human agent,
                the user and human agent are done now and the human agent has put down the call. The user is back on the line with you.

                {user_context}

                The user's preferred language is: **{user_language}**

                Here is a summary of the conversation before the transfer: 
                {call_summary}

                Instructions:
                - Greet the user UNMISTAKABLY in {user_language} by saying - "Looks like you have spoken with our team, how can I help you now?" in {user_language}
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
                System Event: The user was previously speaking with you and requested to be transferred
                to a human agent. However, the human agent was unavailable (DialStatus: {dial_status}).
                The user is back on the line with you.

                {user_context}

                The user's preferred language is: **{user_language}**

                Here is a summary of the conversation before the transfer: 
                {call_summary}

                Instructions:
                - Apologise UNMISTAKABLY in {user_language} by saying exactly - "Sorry, looks like our team couldn't connect, let me help you out in the meantime." in {user_language}.
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
            * **Greeting:** Greet {user_name} and introduce yourself per your persona instructions in the system prompt, then ask whether they'd like to continue in English or switch to another supported language.
            * **STOP HERE:** After asking the language question, END YOUR TURN and stay silent. Do NOT mention any offers, programs, or further details yet. Wait for the user to state their language preference. Only AFTER the user replies do you continue — in their chosen language — with the rest of the flow.
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
                #temperature=0.2,
                session_resumption=types.SessionResumptionConfig(
                    handle=call_state.get("session_resumption_handle")
                ),
                response_modalities=["AUDIO"],
                input_audio_transcription=types.AudioTranscriptionConfig(),
                output_audio_transcription=types.AudioTranscriptionConfig(),
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name="Zephyr"
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
            reset_far_reference(call_state)

            try:
                async with connect_live_with_timeout(client, GEMINI_MODEL, config) as session:
                    if is_reconnect:
                        logger.info("✅ Gemini session resumed successfully")
                        emit_call_event(call_state.get("call_uuid"), "gemini_reconnected")
                    else:
                        logger.info('Connected to Google GenAI Live API')

                    # Task 5.2 — model identity at every session establishment,
                    # first connect and each resumption alike.
                    logger.info(
                        f"🧬 [MODEL] {GEMINI_MODEL} | resumed={is_reconnect} | "
                        f"gemini_reconnect_count={call_state['gemini_reconnect_count']}"
                        f"/{MAX_GEMINI_RECONNECTS} | resumption_handle="
                        f"{'present' if call_state.get('session_resumption_handle') else 'none'}"
                        f" | {describe_gemini_backend()}"
                    )

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
        # Close debug WAV recording
        if call_state.get("debug_wav_writer"):
            try:
                call_state["debug_wav_writer"].close()
                logger.info("🎙️ Debug recording saved.")
            except Exception as e:
                logger.warning(f"⚠️ Error closing debug WAV: {e}")
        if call_state.get("debug_farend_wav_writer"):
            try:
                call_state["debug_farend_wav_writer"].close()
                logger.info(
                    f"🎚️ [FAR-END] recording saved: "
                    f"{call_state['farend_chunks_written']} chunks, "
                    f"{call_state['farend_samples_written']} samples @ "
                    f"{PLIVO_SAMPLE_RATE} Hz"
                )
            except Exception as e:
                logger.warning(f"⚠️ Error closing far-end debug WAV: {e}")
        for kind, aligned_wf in call_state.get("debug_aligned_wav_writers", {}).items():
            try:
                aligned_wf.close()
            except Exception as e:
                logger.warning(f"⚠️ Error closing aligned debug WAV '{kind}': {e}")
        if call_state.get("debug_aligned_wav_writers"):
            logger.info(
                f"🎛️ [ALIGNED-REC] recordings saved: "
                f"{call_state['aligned_frames_written']} frames @ {PLIVO_SAMPLE_RATE} Hz"
            )
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

                # Redesign Part 1: playout-paced far-end reference feed.
                # Consume exactly one far frame per inbound block. Inbound blocks
                # arrive at realtime (Plivo, 20 ms), so this paces the AEC
                # reference to playout instead of the send burst. When nothing is
                # queued (assistant silent, or send hasn't caught up) the far-end
                # is silence — correct: nothing playing means nothing to cancel.
                # far_shadow advances in lockstep with the near-end, so the
                # barge-in gate's near/far windows need no cross-timebase align.
                ref_queue = call_state["farend_ref_queue"]
                if ref_queue:
                    far_ref = ref_queue.popleft()
                    call_state["far_silent_frames"] = 0
                else:
                    far_ref = _FAR_SILENCE_FRAME
                    call_state["far_silent_frames"] = min(
                        call_state["far_silent_frames"] + 1, ECHO_TAIL_FRAMES)
                call_state["aec"].add_far_end(far_ref)
                call_state["far_shadow"].append(far_ref)
                raw_pcm_8k = pcm_8k

                # Acoustic echo cancellation FIRST — strip the AI's echoed voice
                # (speakerphone) before any noise suppression / VAD sees it. When
                # the AI isn't speaking the far-end is silent and this is a
                # near-transparent passthrough.
                pcm_8k = call_state["aec"].process(pcm_8k)
                write_aligned_frames(call_state, raw_pcm_8k, far_ref, pcm_8k, len(ref_queue))
                pcm_8k = guard_aec_output(call_state, raw_pcm_8k, pcm_8k)
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
                
                # Write to debug WAV recording
                if call_state["debug_wav_writer"]:
                    try:
                        call_state["debug_wav_writer"].writeframes(clean_pcm_16k)
                    except Exception:
                        pass
                
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

                echo_gate_decision = None
                if speech_started:
                    # === Corroborated barge-in gate (redesign, concerns a + b) ===
                    # Before a while-speaking onset is treated as caller speech,
                    # check whether it is the assistant's own echo. Only meaningful
                    # while the assistant is playing; when it is silent the far-end
                    # is silence, far_active is False, and the gate always allows
                    # the onset (genuine speech is never blocked). On an echo
                    # verdict the onset is reverted so it never truncates playback
                    # (a), never opens a caller activity window and never reaches
                    # the model (b) — the echo is simply not speech.
                    # Also runs in the echo tail after assistant_speaking drops:
                    # the flag follows the send-side wall clock, but the echo of
                    # the last far frames is still arriving. Without this the
                    # first tail onset opened a phantom caller turn.
                    if ECHO_GATE_ENABLED and (call_state["assistant_speaking"]
                                              or far_window_has_playback(call_state)):
                        echo_gate_decision = evaluate_echo_gate(call_state)
                        latched_before = call_state["echo_latch_erl_db"]
                        echo_suppress, echo_reason = apply_echo_latch(call_state, echo_gate_decision)
                        if echo_reason == "echo-latched":
                            note_echo_latch_hold(call_state, echo_gate_decision, latched_before)
                        elif latched_before is not None and call_state["echo_latch_erl_db"] is None:
                            erl_now = echo_return_db(call_state, echo_gate_decision)
                            if not echo_gate_decision.far_active:
                                cause = "break-far-inactive"
                            elif erl_now > ECHO_MAX_RETURN_DB:
                                cause = "break-ceiling"
                            else:
                                cause = "break-rise"
                            close_echo_latch_episode(call_state, cause, erl_now)
                        if echo_suppress:
                            latched_db = call_state["echo_latch_erl_db"]
                            latched_txt = "none" if latched_db is None else f"{latched_db:.1f}"
                            logger.info(
                                f"🛡️ [ECHO-GATE] suppressed false onset | "
                                f"corr={echo_gate_decision.correlation:.3f} "
                                f">= thr={ECHO_CORR_THRESHOLD} | "
                                f"lag_ms={echo_gate_decision.lag_bins * 10} delay_ms={ECHO_LAG_SEARCH_MS + echo_gate_decision.lag_bins * 10} | "
                                f"near={echo_gate_decision.near_dbfs:.0f}dB "
                                f"far={echo_gate_decision.far_dbfs:.0f}dB | "
                                f"avg_prob={avg_prob:.3f} | reason={echo_reason} | "
                                f"erl_db={echo_return_db(call_state, echo_gate_decision):.1f} "
                                f"latched={latched_txt}"
                            )
                            # Revert the false onset: it was the assistant's echo,
                            # not the caller. Treat the frame as if no onset fired.
                            call_state["is_speaking"] = False
                            call_state["rnnoise_speech_count"] = 0
                            emit_call_event(call_state.get("call_uuid"), "echo_suppressed")
                            continue

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

                        # ---- task 5.3: trigger provenance, logged per decision ----
                        # Read before the trackers below are reset, or delivered_ms
                        # is always zero. Note delivered_ms is quantised to 20 ms:
                        # only whole PLIVO_ULAW_CHUNK_SIZE frames are ever sent.
                        delivered_bytes = call_state["current_utterance_bytes"]
                        # Bytes handed to Plivo -- NOT what the caller heard. Kept
                        # for the buffer-lead measurement, not for classification.
                        queued_ms = delivered_bytes / 8.0
                        # Wall clock since playback started: what the caller heard.
                        playback_start = call_state.get("ai_playback_start_time")
                        if playback_start is not None:
                            heard_ms = (interrupted_time - playback_start).total_seconds() * 1000.0
                            # Cannot have heard more than was actually handed over.
                            heard_ms = max(0.0, min(heard_ms, queued_ms))
                        else:
                            heard_ms = 0.0
                        # Ensure the far-end reference doesn't outpace playout too much.
                        # If the lead is too long, the echo canceller won't work correctly.
                        buffer_lead_ms = queued_ms - heard_ms

                        delivered_ms = heard_ms if TRUNCATION_CLOCK == "wallclock" else queued_ms
                        truncation_class = (
                            "anomalous" if delivered_ms < ANOMALOUS_TRUNCATION_MS else "normal"
                        )
                        logger.info(
                            f"🧭 [TRIGGER] verdict=fired | classification={truncation_class} | "
                            f"avg_prob={avg_prob:.3f} | active_threshold={active_threshold} | "
                            f"onset_frames={active_onset_frames} | "
                            f"rnnoise_speech_count={call_state['rnnoise_speech_count']} | "
                            f"rnnoise_silence_frames={call_state['rnnoise_silence_frames']} | "
                            f"inbound_rms_db={rms_db:.1f} | delivered_ms={delivered_ms:.0f} | "
                            f"heard_ms={heard_ms:.0f} | queued_ms={queued_ms:.0f} | "
                            f"buffer_lead_ms={buffer_lead_ms:.0f} | "
                            f"aec_modeled_path_ms=480 | "
                            f"clock={TRUNCATION_CLOCK} | "
                            f"delivered_bytes={delivered_bytes} | "
                            f"anomalous_below_ms={ANOMALOUS_TRUNCATION_MS} | "
                            f"far_end_evidence={_format_echo_evidence(echo_gate_decision)}"
                        )

                        if truncation_class == "anomalous":
                            ai_text_at_truncation = call_state["ai_text_buffer"]
                            truncated_clusters = leading_clusters(ai_text_at_truncation)
                            logger.info(
                                f"⚠️ [ANOMALY] Truncation | delivered_ms={delivered_ms:.0f} | "
                                f"delivered_bytes={delivered_bytes} | "
                                f"anomalous_below_ms={ANOMALOUS_TRUNCATION_MS} | "
                                f"leading_clusters={truncated_clusters} | "
                                f"ai_text={ai_text_at_truncation.strip()!r} | "
                                f"trigger=vad-while-speaking(avg_prob={avg_prob:.3f}>="
                                f"{active_threshold}, onset_frames={active_onset_frames}) | "
                                f"heard_ms={heard_ms:.0f} | queued_ms={queued_ms:.0f} | "
                                f"buffer_lead_ms={buffer_lead_ms:.0f} | "
                                f"far_end_evidence={_format_echo_evidence(echo_gate_decision)} | "
                                f"model={GEMINI_MODEL} | "
                                f"gemini_reconnect_count={call_state['gemini_reconnect_count']}"
                            )
                            dump_delta_trace(call_state, "anomalous-truncation")
                            # The commit gate delays audio output to fix stuttering,
                            # but is disabled by default to maintain natural sound.
                            if COMMIT_GATE_ENABLED:
                                arm_commit_gate(call_state, ai_text_at_truncation, delivered_ms)
                            else:
                                logger.info(
                                    "🧩 [COMMIT-GATE] not armed | reason=disabled "
                                    "(COMMIT_GATE_ENABLED=0) | diagnostics-only pass; "
                                    "caller-audible behaviour is unchanged"
                                )

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
                            # Clear the echo canceller's buffer to prevent it from
                            # filtering out audio that the caller never actually heard.
                            reset_far_reference(call_state)

                    if not call_state["user_activity_open"]:
                        if not call_state["tool_call_in_progress"]:
                            # Mark activity open before await to prevent duplicate events.
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
                                    
                                elif call.name in course_catalog.TOOL_NAMES:
                                    # Local lookup, no terminal state: the call carries on after the response.
                                    started = time.perf_counter()
                                    result = course_catalog.handle_tool_call(call.name, args_dict)
                                    elapsed_ms = (time.perf_counter() - started) * 1000
                                    chars = course_catalog.response_chars(result)
                                    call_state["catalog_calls"] += 1
                                    call_state["catalog_chars"] += chars
                                    logger.info(
                                        f"🔎 [CATALOG] tool={call.name} "
                                        f"status={course_catalog.result_status(call.name, result)} "
                                        f"chars={chars} ms={elapsed_ms:.1f}"
                                    )
                                    function_responses_to_send.append(
                                        types.FunctionResponse(id=call.id, name=call.name, response=result)
                                    )
                                else:
                                    # Always answer: an unanswered function call leaves Gemini waiting.
                                    logger.warning(f"[⚠️ Unknown tool requested by Gemini: {call.name}]")
                                    function_responses_to_send.append(
                                        types.FunctionResponse(
                                            id=call.id,
                                            name=call.name,
                                            response={"error": f"unknown tool {call.name}"},
                                        )
                                    )
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

                        # Gemini owes the caller an answer to a lookup; endCall and
                        # transferCall are covered by the terminal action deadline.
                        if function_responses_to_send and not any(
                            fr.name in ("endCall", "transferCall") for fr in function_responses_to_send
                        ):
                            call_state["awaiting_model"] = True
                            call_state["model_response_deadline"] = (
                                time.monotonic() + MODEL_RESPONSE_TIMEOUT_SECONDS
                            )

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
                        # Task 5.1 — trace the delta before it is folded into the
                        # buffer, so the join boundary can be classified. The
                        # accumulation itself is unchanged (3.4 / 3.5).
                        record_text_delta(
                            call_state, call_state["ai_text_buffer"], output_transcription.text
                        )
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
                            # Task 5.8. Same clearAudio-desyncs-the-reference case
                            # as the barge-in site above, here on the model's own
                            # confirmed-interruption path.
                            reset_far_reference(call_state)

                        reset_delta_trace(call_state, "model-confirmed-interruption")
                    
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

                        logger.debug(f"🧾 [DELTA] turn summary {delta_trace_verdict(call_state)}")
                        reset_delta_trace(call_state, "turn-complete")
                    
                    model_turn = getattr(server_content, 'model_turn', None)
                    # Task 5.1 replaces the commented-out per-chunk line that used
                    # to sit here with the per-delta trace recorded below.
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
                                # Record every chunk the model delivered, even if dropped,
                                # to help diagnose upstream issues.
                                dropped_locally = (
                                    call_state["interrupting"] or call_state["user_activity_open"]
                                )
                                record_audio_delta(
                                    call_state, part.inline_data.data, not dropped_locally
                                )
                                if dropped_locally:
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
                                            # Task 5.8, for consistency with the two
                                            # sites above: this clearAudio discards
                                            # ringback placeholder audio already
                                            # pushed into the far-end reference.
                                            reset_far_reference(call_state)
                                    await call_state["plivo_output_queue"].put(plivo_ulaw)
                                    call_state["queued_bytes"] += len(plivo_ulaw)
                                    if not call_state["assistant_speaking"]:
                                        # New playback period: a latch from the
                                        # previous one says nothing about this one.
                                        close_echo_latch_episode(call_state, "new-playback")
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
    commit_gate_discard(call_state, "terminal action discarded queued playback")
    await asyncio.sleep(0.2)

    if not call_state["plivo_output_queue"].empty() or call_state["assistant_speaking"]:
        call_state["terminal_action_in_progress"] = False
        call_state["ending_call_phase"] = False
        return False

    if pending_end:
        call_state["end_call_tool_executed"] = True
        call_state["pending_end_call"] = False
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

    async def emit_chunk(chunk):
        """Play out one 160-byte frame. Single send site, so the commit gate's
        release path and the normal path cannot drift apart.

        There is no pacing sleep here and none is introduced: outbound spacing is
        governed by model chunk arrival, and measured inter-frame gaps are 0.0 ms,
        not 20.0 ms (task 4.5). Adding pacing would be a behavioural change to
        requirement 3.9. The redesign paces the AEC reference instead, below.
        """
        # Avoid sending audio to the echo canceller immediately. 
        # Instead, queue it up so it matches the exact time the caller hears it,
        # otherwise the canceller's timing breaks down.
        far_pcm8 = ulaw_to_pcm(chunk)
        call_state["farend_ref_queue"].append(far_pcm8)

        # Task 5.4a — persist the same buffer to disk. Observation only: nothing
        # below this point alters ``chunk``, the canceller's input or the send.
        # The alignment metadata Task 1 had to reverse-engineer from ~1 Hz RMS
        # anchors is written out directly here, so a future analysis reads the
        # timebase instead of rebuilding it. INFO at the same ~1 Hz cadence as the
        # existing [AGC] / [RNNoise] anchors; DEBUG per chunk for full resolution.
        farend_writer = call_state.get("debug_farend_wav_writer")
        if farend_writer:
            try:
                # We pad silent moments with zeros so the recording exactly matches real time.
                # This makes it easy to analyze without complex timestamps.
                now_mono = time.monotonic()
                # setdefault rather than indexing: this whole block sits inside a
                # broad ``except Exception: pass``, so a missing key would silently
                # disable far-end recording instead of failing loudly. Defaulting
                # keeps the recorder working against any call_state shape.
                if call_state.setdefault("farend_t0_mono", None) is None:
                    call_state["farend_t0_mono"] = now_mono
                call_state.setdefault("farend_pad_samples", 0)

                expected = int((now_mono - call_state["farend_t0_mono"]) * PLIVO_SAMPLE_RATE)
                written = call_state["farend_samples_written"]
                gap = expected - written
                # Bound the pad so a clock anomaly cannot write an unbounded file.
                if gap > 0:
                    pad = min(gap, FAREND_MAX_PAD_SAMPLES)
                    farend_writer.writeframes(b"\x00\x00" * pad)
                    call_state["farend_samples_written"] += pad
                    call_state["farend_pad_samples"] += pad
                    if pad < gap:
                        logger.warning(
                            f"🎚️ [FAR-END] pad clamped: wanted {gap} samples, "
                            f"wrote {pad} (cap {FAREND_MAX_PAD_SAMPLES}); "
                            "timebase may drift from here"
                        )

                farend_writer.writeframes(far_pcm8)
                call_state["farend_chunks_written"] += 1
                call_state["farend_samples_written"] += len(far_pcm8) // 2
                alignment = (
                    f"🎚️ [FAR-END] chunk={call_state['farend_chunks_written']} "
                    f"cumulative_samples={call_state['farend_samples_written']} "
                    f"t_mono={now_mono:.6f} t0_mono={call_state['farend_t0_mono']:.6f} "
                    f"rate={PLIVO_SAMPLE_RATE} bytes={len(chunk)} "
                    f"pad_samples_total={call_state['farend_pad_samples']} "
                    f"continuous=1"
                )
                if call_state["farend_chunks_written"] % LOG_EVERY_N_CHUNKS == 1:
                    logger.info(alignment)
                else:
                    logger.debug(alignment)
            except Exception:
                pass

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

        # Task 5.1 — downstream half of the byte counters. A repeated hash is only
        # counted when the frame carries structure: real audio contains runs of
        # identical digital-silence frames, so a naive uniqueness test would flag
        # silence as re-delivery.
        call_state["sent_bytes"] += len(chunk)
        if len(set(chunk)) > 2:
            digest = _short_hash(chunk)
            if digest in call_state["sent_chunk_hashes"]:
                call_state["duplicate_sent_chunk_hashes"] += 1
            else:
                call_state["sent_chunk_hashes"].add(digest)

        audio_delta = {
            "event": "playAudio",
            "media": {
                "contentType": "audio/x-mulaw",
                "sampleRate": 8000,
                "payload": base64.b64encode(chunk).decode("utf-8"),
            }
        }
        await plivo_ws.send(json.dumps(audio_delta))

    def commit_gate_frames(chunk):
        """Task 5.10. Returns the frames to play out for this input frame.

        Not armed -> ``[chunk]``, i.e. exact pass-through with no added latency.
        Armed and sharing the truncated turn's leading clusters -> withhold until
        GRAPHEME_COMMIT_BYTES has accumulated, then release every whole frame
        back to back.
        """
        # The gate engages at most once per utterance. Once the commit window has
        # been satisfied the leading cluster is out whole, so the rest of the turn
        # must pass straight through -- otherwise the gate would keep re-arming
        # every 240 ms and chop the whole turn into bursts.
        if call_state["commit_gate_committed"]:
            return [chunk]

        verdict, shared = commit_gate_verdict(call_state)
        if verdict != call_state["commit_gate_last_verdict"]:
            call_state["commit_gate_last_verdict"] = verdict
            if verdict != "off":
                # One line per transition, so a future reader can see the gate was
                # armed and what it decided — including deciding not to engage.
                logger.info(
                    f"🧩 [COMMIT-GATE] decision={verdict} | "
                    f"shared_leading_clusters={shared} | "
                    f"armed_clusters={call_state['commit_gate_armed_clusters']} | "
                    f"new_clusters={leading_clusters(call_state['ai_text_buffer'])} | "
                    f"commit_bytes={GRAPHEME_COMMIT_BYTES}"
                )
        if verdict == "off":
            return [chunk]

        hold = call_state["commit_gate_hold"]
        if verdict == "release-no-match":
            if not call_state["commit_gate_holding"] and not hold:
                return [chunk]
            logger.info(
                f"🧩 [COMMIT-GATE] released | reason=no-shared-leading-cluster | "
                f"withheld_bytes={len(hold)} | withheld_ms={len(hold) / 8.0:.0f}"
            )
            released = bytes(hold) + chunk
            hold.clear()
            call_state["commit_gate_holding"] = False
            call_state["commit_gate_committed"] = True
            return _whole_frames(released, hold)

        if not call_state["commit_gate_holding"]:
            call_state["commit_gate_holding"] = True
            logger.info(
                f"🧩 [COMMIT-GATE] withholding | reason={verdict} | "
                f"shared_leading_clusters={shared} | "
                f"commit_bytes={GRAPHEME_COMMIT_BYTES}"
            )
        hold.extend(chunk)
        if len(hold) < GRAPHEME_COMMIT_BYTES:
            logger.debug(
                f"🧩 [COMMIT-GATE] held | bytes={len(hold)}/{GRAPHEME_COMMIT_BYTES}"
            )
            return []

        logger.info(
            f"🧩 [COMMIT-GATE] released | reason=commit-window-reached | "
            f"withheld_bytes={len(hold)} | withheld_ms={len(hold) / 8.0:.0f}"
        )
        released = bytes(hold)
        hold.clear()
        call_state["commit_gate_holding"] = False
        call_state["commit_gate_committed"] = True
        return _whole_frames(released, hold)

    def _whole_frames(payload, hold):
        """Split ``payload`` into whole frames, returning the remainder to ``hold``.

        The remainder can only be non-empty if a queued chunk was not a frame
        multiple; keeping it in ``hold`` preserves byte order and byte count.
        """
        whole = len(payload) // PLIVO_ULAW_CHUNK_SIZE * PLIVO_ULAW_CHUNK_SIZE
        if whole < len(payload):
            hold.extend(payload[whole:])
            call_state["commit_gate_holding"] = True
        return [payload[offset:offset + PLIVO_ULAW_CHUNK_SIZE]
                for offset in range(0, whole, PLIVO_ULAW_CHUNK_SIZE)]

    try:
        while True:
            if call_state["terminate_session"]:
                logger.info("Terminating Plivo sender loop")
                break
            try:
                audio = await asyncio.wait_for(call_state["plivo_output_queue"].get(), timeout=PLIVO_SEND_POLL_TIMEOUT)
                out_buffer.extend(audio)
                call_state["commit_gate_idle_polls"] = 0

                while len(out_buffer) >= PLIVO_ULAW_CHUNK_SIZE:
                    if call_state["interrupting"] or call_state["user_activity_open"]:
                        out_buffer.clear()
                        commit_gate_discard(call_state, "barge-in discarded queued playback")
                        break

                    chunk = bytes(out_buffer[:PLIVO_ULAW_CHUNK_SIZE])
                    del out_buffer[:PLIVO_ULAW_CHUNK_SIZE]

                    for frame in commit_gate_frames(chunk):
                        await emit_chunk(frame)

            except asyncio.TimeoutError:
                # Safety valve: Ensures very short turns (less than the commit window delay)
                # are still sent to the caller if the AI has finished its thought.
                if (
                    call_state["commit_gate_hold"]
                    and call_state["plivo_output_queue"].empty()
                    and call_state.get("turn_complete", True)
                ):
                    call_state["commit_gate_idle_polls"] += 1
                    if call_state["commit_gate_idle_polls"] >= COMMIT_GATE_IDLE_RELEASE_POLLS:
                        held = len(call_state["commit_gate_hold"])
                        logger.info(
                            f"🧩 [COMMIT-GATE] released | reason=short-turn-complete | "
                            f"withheld_bytes={held} | withheld_ms={held / 8.0:.0f} | "
                            f"idle_polls={call_state['commit_gate_idle_polls']} | "
                            f"commit_bytes={GRAPHEME_COMMIT_BYTES}"
                        )
                        released = bytes(call_state["commit_gate_hold"])
                        call_state["commit_gate_hold"].clear()
                        call_state["commit_gate_holding"] = False
                        call_state["commit_gate_committed"] = True
                        call_state["commit_gate_idle_polls"] = 0
                        for frame in _whole_frames(released, call_state["commit_gate_hold"]):
                            await emit_chunk(frame)

                if call_state["commit_gate_hold"] and not (
                    call_state.get("pending_end_call") or call_state.get("pending_transfer_call")
                ):
                    # Withheld audio means the utterance is pending, not finished.
                    # Falling through would let the playback-idle branch below flip
                    # ``assistant_speaking`` to False while the assistant is about
                    # to speak -- a behavioural side effect well beyond the
                    # withholding this gate is authorised to perform.
                    continue

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

                        # Task 5.10 — disarm on the first turn that completes past
                        # the commit window without being truncated.
                        if (
                            call_state["commit_gate_armed"]
                            and call_state["current_utterance_bytes"] >= GRAPHEME_COMMIT_BYTES
                        ):
                            disarm_commit_gate(
                                call_state,
                                f"turn completed with {duration_seconds * 1000:.0f} ms delivered "
                                f"(>= {GRAPHEME_COMMIT_MS} ms) and no truncation",
                            )

                        if not call_state.get("greeting_completed", False):
                            logger.info("Initial greeting complete. Microphone is now live.")
                            call_state["greeting_completed"] = True
                            emit_call_event(call_state.get("call_uuid"), "call_connected")

                        call_state["ai_playback_start_time"] = None
                        call_state["current_utterance_bytes"] = 0
                        call_state["assistant_speaking"] = False
                        call_state["interrupting"] = False
                        # Next utterance re-evaluates the gate from scratch.
                        call_state["commit_gate_committed"] = False
                        call_state["commit_gate_last_verdict"] = ""

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

call_ringing_status = {}

@app.route('/plivo-ringing', methods=['GET', 'POST'])
async def plivo_ringing():
    data = await request.form if request.method == 'POST' else request.args
    call_uuid = data.get('CallUUID')
    if call_uuid:
        call_ringing_status[call_uuid] = True
        logger.info(f"🔔 Plivo Ringing Callback: CallUUID={call_uuid} is physically ringing.")
    return "OK", 200

@app.route('/plivo-hangup', methods=['GET', 'POST'])
async def plivo_hangup():
    data = await request.form if request.method == 'POST' else request.args
    call_uuid = data.get('CallUUID')
    hangup_cause = data.get('HangupCause')
    hangup_cause_code = data.get('HangupCauseCode')
    to_number = data.get('To', '')
    
    if to_number.startswith('+91'):
        to_number = to_number[3:]
        
    logger.info(f"☎️ Plivo Hangup Callback: CallUUID={call_uuid}, Cause={hangup_cause} ({hangup_cause_code})")
    
    if hangup_cause == "USER_BUSY":
        # Note: We cannot reliably distinguish between "Switched Off" and "Manually Rejected"
        # because Indian telecom carriers send a 180 Ringing signal (Early Media) while playing 
        # the "switched off" audio message, and then return 486 Busy (3010) after ~21 seconds.
        hangup_cause = "USER_BUSY"
            
    # Clean up state just in case
    if call_uuid in call_ringing_status:
        call_ringing_status.pop(call_uuid, None)
    
    if hangup_cause and hangup_cause != "NORMAL_CLEARING":
        emit_call_event(call_uuid, "call_failed", {"phone": to_number, "error": hangup_cause})
        
    return "OK", 200

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
    hangup_url = f"{PUBLIC_BASE_URL}/plivo-hangup"
    ring_url = f"{PUBLIC_BASE_URL}/plivo-ringing"
    
    try:
        logger.info(f"Initiating outbound call to {target_phone_number} via Dashboard...")
        call_made = await asyncio.to_thread(
            plivo_client.calls.create,
            from_=PLIVO_PHONE_NUMBER,
            to_="+91" + target_phone_number,
            answer_url=answer_url,
            answer_method='GET',
            ring_url=ring_url,
            ring_method='POST',
            hangup_url=hangup_url,
            hangup_method='POST'
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
    logger.info(f"🧬 [MODEL] Gemini {describe_gemini_backend()}")
    logger.info(f"📝 Prompt: {PROMPT_FILE} ({len(RAW_SYSTEM_PROMPT)} chars)")
    if GEMINI_BACKEND == "vertex":
        import google.auth.transport.requests
        load_vertex_credentials().refresh(google.auth.transport.requests.Request())
        logger.info("🔐 Vertex AI credentials loaded and access token obtained")
    import hypercorn.asyncio
    from hypercorn.config import Config as HypercornConfig
    import signal

    hconfig = HypercornConfig()
    hconfig.bind = [f"0.0.0.0:{PORT}"]
    hconfig.graceful_timeout = 0.5

    async def main():
        shutdown_event = asyncio.Event()
        def _signal_handler():
            shutdown_event.set()

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, _signal_handler)
            loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        except NotImplementedError:
            pass

        await hypercorn.asyncio.serve(app, hconfig, shutdown_trigger=shutdown_event.wait)

    asyncio.run(main())