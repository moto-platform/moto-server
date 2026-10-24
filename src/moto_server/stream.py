"""Live stream fan-out for GET /ws/live (moto-analyzer live source, D-081).

One producer publishes StreamBatchDto batches into a LiveHub; every WebSocket client
has its own bounded queue. A slow client never blocks the producer or the other
clients: when its queue is full the oldest batch is dropped and counted.

Protocol (JSON text messages):
- client -> server, first message within HELLO_TIMEOUT_S:
  {"type": "hello", "token": "<MOTO_API_TOKEN>", "channels": [1, 2], "ids": [32, 272]}
  (`channels` and `ids` are optional frame filters; decoded signals are not filtered).
  The token travels in the message, never in the URL.
- server -> client: StreamMessageDto envelopes, {"type": "batch", "payload": ...} and
  every HEARTBEAT_PERIOD_S {"type": "heartbeat", "payload": {"source", "dropped", ...}}.
- Close codes: 1008 bad or missing hello/token, 1011 MOTO_API_TOKEN not configured.

Live sources: "none" (default; heartbeats only) or "mock" (MockCanStreamer, synthetic
data built from defs). The real source, rt-core via the Raspi (D-077), is still open.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import time
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from moto_server.mock_stream import MockCanStreamer
from moto_server.schemas.stream import StreamBatchDto, StreamMessageDto

LIVE_SOURCES = ("none", "mock")
QUEUE_BATCHES = 256  # ~5 s at the mock's 50 Hz
HELLO_TIMEOUT_S = 5.0
HEARTBEAT_PERIOD_S = 5.0
CLOSE_POLICY = 1008
CLOSE_NOT_CONFIGURED = 1011


@dataclass(eq=False)
class Subscriber:
    channels: frozenset[int] | None = None
    ids: frozenset[int] | None = None
    queue: asyncio.Queue[StreamBatchDto] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_BATCHES)
    )
    dropped: int = 0

    def select(self, batch: StreamBatchDto) -> StreamBatchDto:
        if self.channels is None and self.ids is None:
            return batch
        frames = [
            f
            for f in batch.frames
            if (self.channels is None or f.channel in self.channels)
            and (self.ids is None or f.id in self.ids)
        ]
        return batch.model_copy(update={"frames": frames})


class LiveHub:
    """Non-blocking publisher with one bounded queue per subscriber."""

    def __init__(self, source: str = "none") -> None:
        if source not in LIVE_SOURCES:
            raise ValueError(f"unknown live source {source!r}, expected one of {LIVE_SOURCES}")
        self.source = source
        self._subscribers: set[Subscriber] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(
        self, channels: frozenset[int] | None = None, ids: frozenset[int] | None = None
    ) -> Subscriber:
        sub = Subscriber(channels=channels, ids=ids)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def publish(self, batch: StreamBatchDto) -> None:
        for sub in self._subscribers:
            if sub.queue.full():
                sub.queue.get_nowait()
                sub.dropped += 1
            sub.queue.put_nowait(sub.select(batch))

    async def run_mock(self, rate_hz: int = 50) -> None:
        """Producer for source "mock": publishes until cancelled, idle without clients."""
        streamer = MockCanStreamer()
        async for batch in streamer.stream_batches(rate_hz=rate_hz):
            if self._subscribers:
                self.publish(batch)


def _int_set(value: object) -> frozenset[int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(
        isinstance(v, int) and not isinstance(v, bool) for v in value
    ):
        raise ValueError("expected a list of integers")
    return frozenset(value)


def _heartbeat(hub: LiveHub, sub: Subscriber) -> str:
    # A plain dict: through StreamMessageDto's payload union pydantic would coerce it
    # into StreamBatchDto (only timestamp_us is required there) and drop the fields.
    payload = {
        "timestamp_us": int(time.time() * 1_000_000),
        "source": hub.source,
        "dropped": sub.dropped,
    }
    return json.dumps({"type": "heartbeat", "payload": payload})


async def serve_client(websocket: WebSocket, hub: LiveHub, api_token: str | None) -> None:
    await websocket.accept()
    if api_token is None:
        await websocket.close(CLOSE_NOT_CONFIGURED, "MOTO_API_TOKEN is not configured")
        return
    try:
        hello = json.loads(await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT_S))
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise ValueError("first message must be a hello")
        token = hello.get("token")
        # Constant-time comparison so response timing does not leak the token.
        if not isinstance(token, str) or not hmac.compare_digest(
            token.encode(), api_token.encode()
        ):
            raise PermissionError("missing or invalid token")
        channels = _int_set(hello.get("channels"))
        ids = _int_set(hello.get("ids"))
    except (TimeoutError, ValueError, PermissionError):
        await websocket.close(CLOSE_POLICY, "hello with a valid token required")
        return
    except WebSocketDisconnect:
        return

    sub = hub.subscribe(channels=channels, ids=ids)
    try:
        await websocket.send_text(_heartbeat(hub, sub))
        last_beat = time.monotonic()
        while True:
            timeout = max(0.0, HEARTBEAT_PERIOD_S - (time.monotonic() - last_beat))
            try:
                batch = await asyncio.wait_for(sub.queue.get(), timeout)
            except TimeoutError:
                batch = None
            if batch is not None:
                await websocket.send_text(
                    StreamMessageDto(type="batch", payload=batch).model_dump_json()
                )
            if time.monotonic() - last_beat >= HEARTBEAT_PERIOD_S:
                await websocket.send_text(_heartbeat(hub, sub))
                last_beat = time.monotonic()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(sub)


@contextlib.asynccontextmanager
async def running_source(hub: LiveHub):
    """Runs the hub's producer for the lifetime of the app (nothing for "none")."""
    task = asyncio.create_task(hub.run_mock()) if hub.source == "mock" else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
