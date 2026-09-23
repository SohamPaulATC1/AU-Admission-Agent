"""``FakePlivoWS``, ``FakeSession``, ``FakeClock`` and response builders.

These three fakes are what make the audio path testable with no phone call and
no live model. They are driven against the real coroutines in ``app.py``:

    app.stream_plivo_to_gemini(plivo_ws, session, call_state)
    app.stream_gemini_to_plivo(session, plivo_ws, call_state, plivo_client)
    app.send_plivo_audio(plivo_ws, call_state, session, plivo_client)

All three are module-level in app.py (lines 1206, 1465, 1918), not nested inside
``handle_media_stream``, so no extraction is needed to reach them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time as _real_time
from datetime import datetime, timedelta, timezone

from unittest import mock

from tests.harness.appctl import app

IST = timezone(timedelta(hours=5, minutes=30))


# =============================================================================
# FakeClock
# =============================================================================

class FakeClock:
    """A virtual clock for app.py's two time sources.

    app.py reads time two ways:

    * ``time.monotonic()`` / ``time.perf_counter()`` -- deadlines and latency.
    * ``datetime.now(ist_tz)`` -- every ``[TIMING]`` log line and the
      playback-ended arithmetic in ``send_plivo_audio``.

    ``install()`` rebinds ``app.time`` and ``app.datetime`` to shims over this
    clock. It deliberately does NOT patch the event loop's clock, because
    ``asyncio.wait_for`` reads ``loop.time()`` directly and replacing that would
    mean replacing the event loop. The consequence, stated plainly: the 100 ms
    ``PLIVO_SEND_POLL_TIMEOUT`` in ``send_plivo_audio`` still costs 100 ms of
    real time per timeout iteration. Everything app.py *observes* is virtual, so
    branch selection and every logged timestamp stay deterministic.
    """

    def __init__(self, monotonic_start: float = 10_000.0, wall_start: datetime | None = None):
        self._monotonic = float(monotonic_start)
        self._wall = wall_start or datetime(2026, 3, 1, 14, 46, 51, 660_000, tzinfo=IST)

    # -- reads ----------------------------------------------------------------
    def monotonic(self) -> float:
        return self._monotonic

    def perf_counter(self) -> float:
        return self._monotonic

    def time(self) -> float:
        return self._wall.timestamp()

    def now(self, tz=None) -> datetime:
        return self._wall.astimezone(tz) if tz is not None else self._wall

    # -- writes ---------------------------------------------------------------
    def advance(self, seconds: float) -> "FakeClock":
        self._monotonic += float(seconds)
        self._wall = self._wall + timedelta(seconds=float(seconds))
        return self

    def advance_ms(self, milliseconds: float) -> "FakeClock":
        return self.advance(milliseconds / 1000.0)

    # -- installation ---------------------------------------------------------
    @contextlib.contextmanager
    def install(self):
        clock = self

        class _TimeShim:
            monotonic = staticmethod(clock.monotonic)
            perf_counter = staticmethod(clock.perf_counter)
            time = staticmethod(clock.time)

            def __getattr__(self, name):  # anything app.py doesn't override
                return getattr(_real_time, name)

        class _DatetimeShim:
            @staticmethod
            def now(tz=None):
                return clock.now(tz)

            def __getattr__(self, name):
                return getattr(datetime, name)

        with mock.patch.object(app, "time", _TimeShim()), \
                mock.patch.object(app, "datetime", _DatetimeShim()):
            yield clock


# =============================================================================
# FakePlivoWS
# =============================================================================

class FakePlivoWS:
    """Records every JSON frame app.py would have sent to Plivo.

    Records, per frame: the virtual monotonic send time, the parsed frame, and
    for ``playAudio`` the decoded ??-law payload. That is enough to assert the
    160-byte framing (``PLIVO_ULAW_CHUNK_SIZE``) and the inter-frame spacing.

    ``checkpoint`` is recorded generically along with every other event name.
    Worth noting up front: app.py emits only ``playAudio`` and ``clearAudio``
    today -- there is no ``checkpoint`` send anywhere in the file -- so a
    checkpoint assertion would be vacuous on unfixed code.
    """

    def __init__(self, clock: FakeClock | None = None, on_exhausted: str = "none"):
        self.clock = clock or FakeClock()
        self.on_exhausted = on_exhausted  # "none" | "block" | "stop"
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._blocker = asyncio.Event()
        self.frames: list[dict] = []
        self.closed_with: list[int] = []
        self.receive_count = 0

    # -- inbound (app.py calls ``receive``) -----------------------------------
    def queue_raw(self, message: str | None) -> "FakePlivoWS":
        self._inbound.put_nowait(message)
        return self

    def queue_event(self, payload: dict) -> "FakePlivoWS":
        return self.queue_raw(json.dumps(payload))

    def queue_start(self, stream_id: str = "test-stream-id", call_id: str = "test-call-uuid"):
        return self.queue_event({
            "event": "start",
            "start": {"streamId": stream_id, "callId": call_id, "customParameters": {}},
        })

    def queue_media_ulaw(self, ulaw_frame: bytes) -> "FakePlivoWS":
        return self.queue_event({
            "event": "media",
            "media": {"payload": base64.b64encode(ulaw_frame).decode("ascii")},
        })

    def queue_media_pcm8k(self, pcm8k_frame: bytes) -> "FakePlivoWS":
        return self.queue_media_ulaw(app.pcm_to_ulaw(pcm8k_frame))

    def queue_stop(self) -> "FakePlivoWS":
        return self.queue_event({"event": "stop"})

    def queue_disconnect(self) -> "FakePlivoWS":
        return self.queue_raw(None)

    async def receive(self):
        self.receive_count += 1
        if self.on_exhausted == "block":
            # Park on the queue itself so frames fed later still wake the loop.
            # ``release()`` pushes ``None``, which app.py reads as a disconnect.
            return await self._inbound.get()
        if self._inbound.empty():
            if self.on_exhausted == "stop":
                return json.dumps({"event": "stop"})
            return None
        return self._inbound.get_nowait()

    def release(self) -> None:
        self._inbound.put_nowait(None)
        self._blocker.set()

    # -- outbound (app.py calls ``send``) -------------------------------------
    async def send(self, text: str) -> None:
        frame = json.loads(text)
        record = {
            "t": self.clock.monotonic(),
            "event": frame.get("event"),
            "frame": frame,
        }
        if frame.get("event") == "playAudio":
            record["ulaw"] = base64.b64decode(frame["media"]["payload"])
            record["sample_rate"] = frame["media"]["sampleRate"]
            record["content_type"] = frame["media"]["contentType"]
        self.frames.append(record)

    async def close(self, code: int = 1000) -> None:
        self.closed_with.append(code)

    # -- accessors ------------------------------------------------------------
    @property
    def events(self) -> list[str]:
        return [record["event"] for record in self.frames]

    @property
    def play_audio_frames(self) -> list[dict]:
        return [record for record in self.frames if record["event"] == "playAudio"]

    @property
    def play_audio_payloads(self) -> list[bytes]:
        return [record["ulaw"] for record in self.play_audio_frames]

    @property
    def outbound_ulaw(self) -> bytes:
        return b"".join(self.play_audio_payloads)

    @property
    def send_times(self) -> list[float]:
        return [record["t"] for record in self.play_audio_frames]

    @property
    def inter_frame_gaps_ms(self) -> list[float]:
        times = self.send_times
        return [round((b - a) * 1000.0, 6) for a, b in zip(times, times[1:])]

    @property
    def clear_audio_count(self) -> int:
        return self.events.count("clearAudio")

    @property
    def clear_audio_times(self) -> list[float]:
        return [r["t"] for r in self.frames if r["event"] == "clearAudio"]

    @property
    def checkpoint_frames(self) -> list[dict]:
        return [record for record in self.frames if record["event"] == "checkpoint"]

    def event_sequence(self) -> list[str]:
        """Events with runs of ``playAudio`` collapsed, e.g.
        ``['playAudio x7', 'clearAudio']`` -- the shape assertions read better."""
        out: list[str] = []
        for event in self.events:
            if out and out[-1].startswith(event + " x"):
                head, count = out[-1].rsplit(" x", 1)
                out[-1] = f"{head} x{int(count) + 1}"
            elif out and out[-1] == event:
                out[-1] = f"{event} x2"
            else:
                out.append(event)
        return out


# =============================================================================
# FakeSession -- scripted Gemini Live responses
# =============================================================================

class _Bag:
    """Attribute bag. app.py reads everything off responses with ``getattr``,
    except ``response.tool_call``, which is accessed directly -- so every
    response object must carry that attribute."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"_Bag({self.__dict__!r})"


def response(**kwargs) -> _Bag:
    base = {
        "tool_call": None,
        "server_content": None,
        "usage_metadata": None,
        "session_resumption_update": None,
        "go_away": None,
    }
    base.update(kwargs)
    return _Bag(**base)


def server_content(**kwargs) -> _Bag:
    base = {
        "input_transcription": None,
        "output_transcription": None,
        "interrupted": False,
        "turn_complete": False,
        "model_turn": None,
    }
    base.update(kwargs)
    return _Bag(**base)


def resp_audio(*pcm24_chunks: bytes) -> _Bag:
    """A ``model_turn`` carrying ``inline_data`` audio at ``GEMINI_OUTPUT_RATE``."""
    parts = [_Bag(inline_data=_Bag(data=chunk, mime_type="audio/pcm;rate=24000"))
             for chunk in pcm24_chunks]
    return response(server_content=server_content(model_turn=_Bag(parts=parts)))


def resp_output_transcription(text: str) -> _Bag:
    return response(server_content=server_content(output_transcription=_Bag(text=text)))


def resp_input_transcription(text: str) -> _Bag:
    return response(server_content=server_content(input_transcription=_Bag(text=text)))


def resp_interrupted() -> _Bag:
    return response(server_content=server_content(interrupted=True))


def resp_turn_complete() -> _Bag:
    return response(server_content=server_content(turn_complete=True))


def resp_tool_call(name: str, args: dict, call_id: str = "call-1") -> _Bag:
    call = _Bag(name=name, args=args, id=call_id)
    return response(tool_call=_Bag(function_calls=[call]))


def resp_usage(total: int | None = None, prompt=(), resp=()) -> _Bag:
    def details(pairs):
        return [_Bag(modality=modality, token_count=count) for modality, count in pairs]

    return response(usage_metadata=_Bag(
        total_token_count=total,
        prompt_tokens_details=details(prompt),
        response_tokens_details=details(resp),
    ))


def resp_resumption(handle: str, resumable: bool = True) -> _Bag:
    return response(session_resumption_update=_Bag(resumable=resumable, new_handle=handle))


def resp_go_away(time_left: str = "5s") -> _Bag:
    return response(go_away=_Bag(time_left=time_left))


class RaiseDisconnected:
    """Script sentinel: ``session.receive()`` raises ``GeminiSessionDisconnected``."""

    def __init__(self, message: str = "scripted disconnect"):
        self.message = message


class Callback:
    """Script sentinel: run ``fn()`` between two responses.

    This is how a test interleaves its own side effects -- advancing the clock,
    flipping ``interrupting``, injecting a local re-queue -- into the middle of a
    scripted turn without racing the coroutine under test.
    """

    def __init__(self, fn):
        self.fn = fn


class FakeSession:
    """Scripted ``server_content`` source that also records what app.py sends."""

    def __init__(self, script=(), on_exhausted: str = "block"):
        self.script = list(script)
        self.on_exhausted = on_exhausted  # "block" | "stop" | "disconnect"
        self.sent: list[tuple[str, object]] = []
        self.receive_calls = 0
        self._blocker = asyncio.Event()

    # -- app.py -> session ----------------------------------------------------
    async def send_realtime_input(self, **kwargs) -> None:
        if "activity_start" in kwargs:
            self.sent.append(("activityStart", None))
        elif "activity_end" in kwargs:
            self.sent.append(("activityEnd", None))
        elif "audio" in kwargs:
            blob = kwargs["audio"]
            self.sent.append(("audio", bytes(blob.data)))
        elif "text" in kwargs:
            self.sent.append(("text", kwargs["text"]))
        else:  # pragma: no cover - defensive
            self.sent.append(("realtime_input", kwargs))

    async def send_client_content(self, **kwargs) -> None:
        self.sent.append(("client_content", kwargs))

    async def send_tool_response(self, **kwargs) -> None:
        self.sent.append(("tool_response", kwargs))

    # -- session -> app.py ----------------------------------------------------
    async def _generate(self):
        while self.script:
            item = self.script.pop(0)
            if isinstance(item, RaiseDisconnected):
                raise app.GeminiSessionDisconnected(item.message)
            if isinstance(item, Callback):
                result = item.fn()
                if asyncio.iscoroutine(result):
                    await result
                continue
            yield item
            await asyncio.sleep(0)

    def receive(self):
        """``stream_gemini_to_plivo`` wraps ``async for response in
        session.receive()`` in ``while True``, so an exhausted script must not
        return an empty iterator -- that would spin the loop hot. Default
        ``on_exhausted='block'`` parks forever so the test can cancel the task."""
        self.receive_calls += 1
        if self.receive_calls == 1:
            return self._generate()
        return self._after_exhaustion()

    async def _after_exhaustion(self):
        if self.on_exhausted == "disconnect":
            raise app.GeminiSessionDisconnected("script exhausted")
        if self.on_exhausted == "stop":
            return
        await self._blocker.wait()
        return
        yield  # pragma: no cover - makes this an async generator

    def release(self) -> None:
        self._blocker.set()

    # -- accessors ------------------------------------------------------------
    @property
    def sent_kinds(self) -> list[str]:
        return [kind for kind, _ in self.sent]

    @property
    def audio_sent(self) -> list[bytes]:
        return [payload for kind, payload in self.sent if kind == "audio"]

    @property
    def audio_bytes_sent(self) -> bytes:
        return b"".join(self.audio_sent)

    def count(self, kind: str) -> int:
        return self.sent_kinds.count(kind)


class FakePlivoClient:
    """Stand-in for ``plivo.RestClient``. Records terminal API calls."""

    def __init__(self):
        self.deleted: list[str] = []
        self.transferred: list[dict] = []
        self.calls = self

    def delete(self, call_uuid=None):
        self.deleted.append(call_uuid)

    def transfer(self, **kwargs):
        self.transferred.append(kwargs)


# =============================================================================
# task drivers
# =============================================================================

async def run_until_idle(coro_fn, *, timeout: float = 15.0):
    """Run a coroutine to completion with a hard timeout.

    Used for the coroutines that terminate on their own (``stream_plivo_to_gemini``
    once the inbound queue is exhausted).
    """
    return await asyncio.wait_for(coro_fn, timeout=timeout)


@contextlib.asynccontextmanager
async def background(coro):
    """Start ``coro`` as a task and guarantee cancellation on exit."""
    task = asyncio.ensure_future(coro)
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def drain_queue(call_state, *, idle_polls: int = 3, poll: float = 0.005):
    """Wait until ``plivo_output_queue`` has been empty for a few polls."""
    idle = 0
    while idle < idle_polls:
        if call_state["plivo_output_queue"].empty():
            idle += 1
        else:
            idle = 0
        await asyncio.sleep(poll)


class InboundDriver:
    """Feed media frames into a running ``stream_plivo_to_gemini``, one at a time.

    ``stream_plivo_to_gemini`` is a single ``while True`` loop over
    ``plivo_ws.receive()``, so the only way to observe per-frame VAD state --
    which is what "cuts at 4 frames / 80 ms" means -- is to hand it one frame,
    wait for ``chunk_count`` to advance, and snapshot. ``chunk_count`` is
    incremented once per processed media frame at app.py 1340.

    A 320-byte PCM16 frame is exactly one AEC block (``frame_size=160`` samples),
    so ``aec.process`` never returns empty and never swallows a frame -- the
    ``continue`` at app.py 1241-1243 does not fire for these inputs.
    """

    def __init__(self, call_state, plivo_ws: FakePlivoWS, session: FakeSession, *, timeout: float = 30.0):
        self.call_state = call_state
        self.ws = plivo_ws
        self.session = session
        self.timeout = timeout
        self.task: asyncio.Task | None = None
        self.snapshots: list[dict] = []

    async def __aenter__(self) -> "InboundDriver":
        self.ws.on_exhausted = "block"
        self.task = asyncio.ensure_future(
            app.stream_plivo_to_gemini(self.ws, self.session, self.call_state)
        )
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc_info):
        self.call_state["terminate_session"] = True
        self.ws.release()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        return False

    async def feed_pcm8(self, frame_pcm8: bytes) -> dict:
        before = self.call_state["chunk_count"]
        self.ws.queue_media_pcm8k(frame_pcm8)
        deadline = _real_time.monotonic() + self.timeout
        while self.call_state["chunk_count"] == before:
            if self.task is not None and self.task.done():
                self.task.result()
                raise AssertionError("stream_plivo_to_gemini ended before the frame was processed")
            if _real_time.monotonic() > deadline:
                raise AssertionError("timed out waiting for the inbound frame to be processed")
            await asyncio.sleep(0.001)
        await asyncio.sleep(0)
        snapshot = self.snapshot()
        self.snapshots.append(snapshot)
        return snapshot

    def snapshot(self) -> dict:
        return {
            "chunk_index": self.call_state["chunk_count"] - 1,
            "is_speaking": self.call_state["is_speaking"],
            "speech_count": self.call_state["rnnoise_speech_count"],
            "silence_frames": self.call_state["rnnoise_silence_frames"],
            "assistant_speaking": self.call_state["assistant_speaking"],
            "user_activity_open": self.call_state["user_activity_open"],
            "preroll_bytes": len(self.call_state["preroll_pcm16"]),
            "plivo_events": list(self.ws.events),
            "session_sent": list(self.session.sent_kinds),
        }

    def first_frame_index_where(self, predicate) -> int | None:
        """Index (0-based, in frames fed) of the first snapshot satisfying ``predicate``."""
        for position, snapshot in enumerate(self.snapshots):
            if predicate(snapshot):
                return position
        return None


async def run_gemini_output(session: FakeSession, plivo_ws, call_state, plivo_client,
                            *, settle: float = 0.15):
    """Drive ``stream_gemini_to_plivo`` over a finite script, then stop it.

    The coroutine never returns on its own for a finite script (``while True``
    around ``session.receive()``), so it is cancelled after the script has been
    consumed and the loop has settled.
    """
    task = asyncio.ensure_future(
        app.stream_gemini_to_plivo(session, plivo_ws, call_state, plivo_client)
    )
    try:
        deadline = _real_time.monotonic() + 10.0
        while session.script and not task.done() and _real_time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        await asyncio.sleep(settle)
        if task.done():
            return task.result()
        return None
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


class SenderHarness:
    """Drives ``send_plivo_audio`` with a virtual clock.

    ``send_plivo_audio`` has no pacing sleep: it blocks on
    ``asyncio.wait_for(queue.get(), timeout=PLIVO_SEND_POLL_TIMEOUT)`` and sends
    every 160-byte frame back to back as soon as bytes are available. The only
    real-time cost is the 100 ms poll timeout, which ``asyncio.wait_for`` reads
    off the event loop clock and therefore cannot be virtualised without
    replacing the loop.
    """

    def __init__(self, call_state, clock: FakeClock, plivo_ws: FakePlivoWS,
                 session: FakeSession, plivo_client):
        self.call_state = call_state
        self.clock = clock
        self.ws = plivo_ws
        self.session = session
        self.plivo_client = plivo_client
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> "SenderHarness":
        self.task = asyncio.ensure_future(
            app.send_plivo_audio(self.ws, self.call_state, self.session, self.plivo_client)
        )
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc_info):
        self.call_state["terminate_session"] = True
        await asyncio.sleep(0.12)
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        timer = self.call_state.get("silence_timer_task")
        if timer is not None and not timer.done():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await timer
        return False

    async def enqueue(self, *chunks: bytes) -> None:
        for chunk in chunks:
            self.call_state["plivo_output_queue"].put_nowait(chunk)

    async def wait_sent(self, frame_count: int, *, timeout: float = 10.0) -> None:
        deadline = _real_time.monotonic() + timeout
        while len(self.ws.play_audio_frames) < frame_count:
            if _real_time.monotonic() > deadline:
                raise AssertionError(
                    f"expected {frame_count} playAudio frames, saw {len(self.ws.play_audio_frames)}"
                )
            await asyncio.sleep(0.002)

    async def settle(self, polls: int = 4) -> None:
        await drain_queue(self.call_state, idle_polls=polls)
        await asyncio.sleep(0.02)

    async def finish_utterance(self) -> None:
        """Let the timeout branch of ``send_plivo_audio`` declare playback over.

        Requires the virtual wall clock to be past
        ``ai_playback_start_time + current_utterance_bytes / 8000`` -- otherwise
        app.py 1970 takes the ``continue`` and the branch never completes.
        """
        await self.settle()
        duration = self.call_state["current_utterance_bytes"] / 8000.0
        self.clock.advance(duration + 0.001)
        await asyncio.sleep(app.PLIVO_SEND_POLL_TIMEOUT * 2 + 0.08)
