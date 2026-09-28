"""Side-effect-contained import of ``app``, plus the ``call_state`` factory.

Importing ``app`` at module scope has four side effects that must be contained
before the import happens, or the test suite would mutate production artefacts:

1. ``logging.basicConfig(handlers=[FileHandler("Gemini_Assistant.log"), ...])``
   would append test output to the production log -- the very log task 1's
   measurement was derived from. ``basicConfig`` is a no-op when the root logger
   already has a handler, so we install a ``NullHandler`` first.
2. ``initialize_transfer_context_store()`` runs at import and issues a DELETE
   against ``TRANSFER_CONTEXT_DB_PATH``. We repoint it at a temp file. The
   ``.env`` file is NOT modified; ``load_dotenv()`` does not override variables
   that are already present in ``os.environ``, so setting it here wins.
3. ``plivo.RestClient(...)`` is constructed at module scope. It does no network
   I/O on construction, so it is harmless.
4. ``load_disclaimer()`` reads ``playback_audio_files/recorded1.wav`` read-only.

Nothing here modifies ``app.py``, ``aec.py``, ``requirements.txt`` or ``.env``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import re
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- containment, before ``import app`` --------------------------------------
logging.getLogger().addHandler(logging.NullHandler())
os.environ["TRANSFER_CONTEXT_DB_PATH"] = os.path.join(
    tempfile.gettempdir(), "bgsf_offline_tests_transfer_context.sqlite3"
)

import app  # noqa: E402  (import must follow the containment above)

from aec import AcousticEchoCanceller  # noqa: E402
from pyrnnoise import RNNoise  # noqa: E402

APP_PY = os.path.join(REPO_ROOT, "app.py")
AEC_PY = os.path.join(REPO_ROOT, "aec.py")


# =============================================================================
# call_state
# =============================================================================
# app.py builds ``call_state`` as a dict literal inline inside
# ``handle_media_stream`` (app.py 825-920). There is no factory function to
# call, and extracting one would mean editing app.py, which this pass does not
# do. So the literal is mirrored here, and ``test_harness_smoke`` asserts the
# mirrored key set still matches the literal in app.py's source -- that guard is
# what catches drift without touching production code.

def new_call_state(
    *,
    phone_number: str = "+910000000000",
    is_returning_from_transfer: bool = False,
    with_denoiser: bool = True,
    with_aec: bool = True,
    **overrides,
) -> dict:
    """Mirror of the ``call_state`` literal at app.py 825-920.

    ``with_denoiser`` / ``with_aec`` exist because ``RNNoise(48000)`` and
    ``AcousticEchoCanceller`` are the expensive members; tests that never touch
    the inbound DSP chain can skip them.
    """
    state = {
        "from_number": phone_number,
        "is_returning_from_transfer": is_returning_from_transfer,
        "playing_disclaimer": True,
        "stream_id": None,
        "call_uuid": None,
        "public_base_url": app.PUBLIC_BASE_URL,

        "greeting_completed": False,

        "user_text_buffer": "",
        "ai_text_buffer": "",
        "conversation_log": [],

        "aec": AcousticEchoCanceller(frame_size=app.PLIVO_ULAW_CHUNK_SIZE) if with_aec else None,

        "denoiser": RNNoise(sample_rate=48000) if with_denoiser else None,
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

        "ai_playback_start_time": None,
        "current_utterance_bytes": 0,
        "turn_complete": True,

        "silence_timer_task": None,
        "silence_followup_count": 0,

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

        "analytics_saved": False,
        "skip_finally_analytics": False,

        "pending_transfer_call": False,
        "transfer_call_tool_executed": False,
        "transfer_summary": "",

        "preroll_pcm16": bytearray(),
        "gemini_input_buffer": bytearray(),
        "plivo_output_queue": asyncio.Queue(),

        # Redesign Part 1: playout-paced far-end reference (see app.py).
        "farend_ref_queue": collections.deque(),
        "far_shadow": collections.deque(maxlen=app.FAR_SHADOW_FRAMES),
        "far_silent_frames": app.ECHO_TAIL_FRAMES,
        "echo_latch_erl_db": None,
        "latch_episode": None,
        "latch_stats": app.new_latch_stats(),

        "closing": False,
        "tool_call_in_progress": False,

        "session_resumption_handle": None,
        "go_away_received": False,
        "gemini_reconnect_count": 0,

        "tokens_text_in": 0,
        "tokens_text_out": 0,
        "tokens_audio_in": 0,
        "tokens_audio_out": 0,
        "last_usage_total_token_count": None,
        "usage_total_updates": 0,
        "usage_total_non_monotonic_count": 0,

        "debug_wav_writer": None,

        # --- task 5.4a: far-end debug recording (8 kHz, paired with the 16 kHz
        # inbound writer above). ``None`` here means the recorder is off, which is
        # the state every offline test runs in unless it opens a writer itself.
        "debug_farend_wav_writer": None,
        "farend_samples_written": 0,
        "farend_chunks_written": 0,
        "farend_t0_mono": None,
        "farend_pad_samples": 0,

        # --- TEST1 follow-up: aligned inbound-loop recordings (nearraw / farref /
        # aecout, 8 kHz). Empty dict = recorders off, the offline default.
        "debug_aligned_wav_writers": {},
        "aligned_frames_written": 0,
        "aligned_near_samples_written": 0,
        "aligned_aec_samples_written": 0,

        # --- TEST2 follow-up: AEC output guard counters.
        "aec_guard_frames_checked": 0,
        "aec_guard_fallback_frames": 0,

        # --- task 5.1: per-delta trace and the three per-turn byte counters.
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

        # --- task 5.10: leading-fragment commit gate, inert until armed.
        "commit_gate_armed": False,
        "commit_gate_armed_text": "",
        "commit_gate_armed_clusters": [],
        "commit_gate_hold": bytearray(),
        "commit_gate_holding": False,
        "commit_gate_idle_polls": 0,
        "commit_gate_last_verdict": "",
        "commit_gate_committed": False,
    }
    state.update(overrides)
    return state


def live_call_state(**overrides) -> dict:
    """``new_call_state`` with the flags a mid-call, post-greeting state carries.

    ``playing_disclaimer`` False and ``greeting_completed`` True are both
    required before ``stream_plivo_to_gemini`` will act on a media frame: the
    first short-circuits at app.py 1231, the second at app.py 1345.
    """
    base = {
        "playing_disclaimer": False,
        "greeting_completed": True,
        "stream_id": "test-stream-id",
        "call_uuid": "test-call-uuid",
    }
    base.update(overrides)
    return new_call_state(**base)


_CALL_STATE_LITERAL = re.compile(
    r"^    call_state = \{\n(.*?)^    \}\n", re.DOTALL | re.MULTILINE
)


def call_state_keys_from_app_source() -> list[str]:
    """Scrape the key names out of app.py's ``call_state`` dict literal.

    Drift guard for ``new_call_state``. Deliberately source-level: it must fail
    loudly if app.py gains or loses a state field, since every golden record in
    this suite is built on that dict.
    """
    with open(APP_PY, "r", encoding="utf-8") as handle:
        source = handle.read()
    match = _CALL_STATE_LITERAL.search(source)
    if match is None:  # pragma: no cover - guarded by the smoke test
        raise AssertionError("could not locate the call_state dict literal in app.py")
    body = match.group(1)
    # Top-level keys only: exactly two levels of indentation, then a quoted key.
    return re.findall(r'^        "([^"]+)"\s*:', body, re.MULTILINE)


# =============================================================================
# log capture
# =============================================================================

#: Bracketed tags carried by the diagnostic log lines added by tasks 5.1-5.4a and
#: 5.10. Task 5.13 names these as permitted additions that the task-4 golden
#: records exclude, so ``LogCapture.existing_lines`` filters on them rather than
#: re-baselining the pinned sequences of existing lines.
DIAGNOSTIC_LOG_TAGS = (
    "[ANOMALY]",      # 5.3 anomalous truncation
    "[TRIGGER]",      # 5.3 per-decision trigger provenance
    "[DELTA]",        # 5.1 per-delta trace, DEBUG
    "[DELTA-TRACE]",  # 5.1 trace dump promoted to INFO on an anomaly
    "[COMMIT-GATE]",  # 5.10 arm / withhold / release / discard / disarm
    "[MODEL]",        # 5.2 model identity
    "[FAR-END]",      # 5.4a far-end recorder and its alignment metadata
    "[ALIGNED-REC]",  # TEST1 follow-up: aligned nearraw/farref/aecout recorders
    "[AEC-GUARD]",    # TEST2 follow-up: AEC output louder than input, raw passed
    "[LATCH-HOLD]",   # onset held only by the echo latch
    "[LATCH-END]",    # how a latch episode ended (broken vs dropped)
    "[LATCH]",        # per-call latch summary in call stats
)


def is_diagnostic_line(line: str) -> bool:
    return any(tag in line for tag in DIAGNOSTIC_LOG_TAGS)


class LogCapture:
    """Records ``app``'s log lines in emission order.

    The ordered sequence of existing log lines is itself a golden record
    (task 4), so this captures the rendered message, not the format string.

    The default level is ``INFO`` rather than ``DEBUG``, and deliberately so:
    production runs at ``logging.basicConfig(level=logging.INFO)`` (app.py 31), so
    an ``INFO`` capture is what the golden records actually describe. Task 5.1's
    per-delta trace is emitted at ``DEBUG`` precisely so it stays out of normal
    operation; a test that wants to see it asks for ``LogCapture(level=DEBUG)``.
    """

    def __init__(self, logger=None, level=logging.INFO):
        self._logger = logger if logger is not None else app.logger
        self._level = level
        self.records: list[logging.LogRecord] = []
        self._handler: logging.Handler | None = None

    def __enter__(self) -> "LogCapture":
        capture = self

        class _Handler(logging.Handler):
            def emit(self, record):  # noqa: D401
                capture.records.append(record)

        self._handler = _Handler(level=self._level)
        self._previous_level = self._logger.level
        self._logger.setLevel(self._level)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous_level)
        return False

    # -- accessors ------------------------------------------------------------
    @property
    def lines(self) -> list[str]:
        return [record.getMessage() for record in self.records]

    @property
    def existing_lines(self) -> list[str]:
        """``lines`` with the new diagnostic additions removed.

        This is what task 4's preservation assertions compare against: every
        pre-existing line keeps its exact format and position, and the additions
        are accounted for separately.
        """
        return [line for line in self.lines if not is_diagnostic_line(line)]

    @property
    def diagnostic_lines(self) -> list[str]:
        return [line for line in self.lines if is_diagnostic_line(line)]

    def matching(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle in line]

    def contains(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)

    def index_of(self, needle: str) -> int:
        for position, line in enumerate(self.lines):
            if needle in line:
                return position
        raise AssertionError(f"no captured log line contains {needle!r}\nlines={self.lines}")

    def order_of(self, *needles: str) -> list[int]:
        return [self.index_of(needle) for needle in needles]


@contextlib.contextmanager
def silence_sse():
    """Neutralise ``emit_call_event`` fan-out.

    Not strictly required -- ``call_event_subscribers`` is empty in tests -- but
    it keeps a stray dashboard subscriber from making a test order-dependent.
    """
    saved = app.call_event_subscribers
    app.call_event_subscribers = set()
    try:
        yield
    finally:
        app.call_event_subscribers = saved
