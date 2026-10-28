"""GET /ws/live and the LiveHub fan-out (vehicle_server.stream)."""

from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vehicle_server import stream
from vehicle_server.api import create_app
from vehicle_server.config import Settings
from vehicle_server.schemas.stream import CanFrameDto, StreamBatchDto

HELLO = {"type": "hello", "token": "test-token"}


def _batch(ts: int, frames: list[tuple[int, int]] | None = None) -> StreamBatchDto:
    return StreamBatchDto(
        timestamp_us=ts,
        frames=[
            CanFrameDto(timestamp_us=ts, channel=ch, id=fid, dlc=1, data_hex="00")
            for ch, fid in (frames or [])
        ],
    )


def _next_batch(ws) -> dict:
    for _ in range(50):
        msg = json.loads(ws.receive_text())
        if msg["type"] == "batch":
            return msg["payload"]
    raise AssertionError("no batch received")


@pytest.fixture
def mock_settings(settings: Settings) -> Settings:
    return dataclasses.replace(settings, live_source="mock")


# ------------------------------------------------------------------ hub


def test_hub_rejects_unknown_source():
    with pytest.raises(ValueError):
        stream.LiveHub("kuksa")


def test_slow_subscriber_drops_oldest_without_blocking():
    hub = stream.LiveHub()
    sub = hub.subscribe()
    for ts in range(stream.QUEUE_BATCHES + 10):
        hub.publish(_batch(ts))
    assert sub.queue.qsize() == stream.QUEUE_BATCHES
    assert sub.dropped == 10
    assert sub.queue.get_nowait().timestamp_us == 10  # the oldest 10 went first


def test_subscriber_filters_frames_by_channel_and_id():
    hub = stream.LiveHub()
    by_channel = hub.subscribe(channels=frozenset({2}))
    by_id = hub.subscribe(ids=frozenset({0x110}))
    everything = hub.subscribe()
    hub.publish(_batch(1, [(1, 0x7E8), (2, 0x020), (2, 0x110)]))
    assert [f.id for f in by_channel.queue.get_nowait().frames] == [0x020, 0x110]
    assert [f.id for f in by_id.queue.get_nowait().frames] == [0x110]
    assert len(everything.queue.get_nowait().frames) == 3


def test_unsubscribe_stops_delivery():
    hub = stream.LiveHub()
    sub = hub.subscribe()
    hub.unsubscribe(sub)
    hub.publish(_batch(1))
    assert sub.queue.empty()
    assert hub.subscriber_count == 0


# ------------------------------------------------------------------ endpoint


def test_wrong_token_is_closed_with_policy_code(settings: Settings):
    with TestClient(create_app(settings)) as client, client.websocket_connect("/ws/live") as ws:
        ws.send_text(json.dumps({"type": "hello", "token": "wrong"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == stream.CLOSE_POLICY


def test_first_message_must_be_hello(settings: Settings):
    with TestClient(create_app(settings)) as client, client.websocket_connect("/ws/live") as ws:
        ws.send_text(json.dumps({"type": "subscribe", "token": "test-token"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == stream.CLOSE_POLICY


def test_bad_filter_is_closed_with_policy_code(settings: Settings):
    with TestClient(create_app(settings)) as client, client.websocket_connect("/ws/live") as ws:
        ws.send_text(json.dumps({**HELLO, "ids": ["0x20"]}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == stream.CLOSE_POLICY


def test_no_configured_token_closes(settings: Settings):
    open_settings = dataclasses.replace(settings, api_token=None)
    with (
        TestClient(create_app(open_settings)) as client,
        client.websocket_connect("/ws/live") as ws,
    ):
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == stream.CLOSE_NOT_CONFIGURED


def test_source_none_sends_heartbeat_only(settings: Settings):
    with TestClient(create_app(settings)) as client, client.websocket_connect("/ws/live") as ws:
        ws.send_text(json.dumps(HELLO))
        beat = json.loads(ws.receive_text())
    assert beat["type"] == "heartbeat"
    assert beat["payload"]["source"] == "none"
    assert beat["payload"]["dropped"] == 0


def test_mock_source_streams_batches_and_cleans_up(mock_settings: Settings):
    app = create_app(mock_settings)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            ws.send_text(json.dumps(HELLO))
            beat = json.loads(ws.receive_text())
            assert beat["payload"]["source"] == "mock"
            payload = _next_batch(ws)
            assert payload["frames"], "mock batch without frames"
            assert {"id", "channel", "data_hex", "timestamp_us"} <= payload["frames"][0].keys()
            assert app.state.live_hub.subscriber_count == 1
        for _ in range(100):  # the server notices the disconnect on its next send
            if app.state.live_hub.subscriber_count == 0:
                break
            client.get("/health")
        assert app.state.live_hub.subscriber_count == 0


def test_mock_source_applies_the_hello_filter(mock_settings: Settings):
    with TestClient(create_app(mock_settings)) as client:
        with client.websocket_connect("/ws/live") as ws:
            ws.send_text(json.dumps({**HELLO, "channels": [99]}))
            payload = _next_batch(ws)
    assert payload["frames"] == []
