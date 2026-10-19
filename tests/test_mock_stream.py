"""Unit tests for schemas.stream and mock_stream module."""

from __future__ import annotations

from pathlib import Path

import pytest

from moto_server.mock_stream import MockCanStreamer
from moto_server.schemas.stream import (
    BusStatsDto,
    CanFrameDto,
    DecodedSignalDto,
    DtcRecordDto,
    StreamBatchDto,
    StreamMessageDto,
)


def test_can_frame_dto() -> None:
    frame = CanFrameDto(
        timestamp_us=1000000,
        channel=2,
        id=0x020,
        is_extended=False,
        dlc=8,
        data_hex="0102030405060708",
        rxtx="rx",
    )
    assert frame.bytes_list() == [1, 2, 3, 4, 5, 6, 7, 8]
    assert frame.id == 32
    assert frame.dlc == 8

    empty_frame = CanFrameDto(
        timestamp_us=1000000,
        channel=1,
        id=0x100,
        dlc=0,
        data_hex="",
    )
    assert empty_frame.bytes_list() == []


def test_decoded_signal_dto() -> None:
    sig = DecodedSignalDto(
        name="ENGINE_SPEED",
        value=3250.0,
        unit="rpm",
        timestamp_us=2000000,
        raw_value=3250,
        is_valid=True,
    )
    assert sig.name == "ENGINE_SPEED"
    assert sig.value == 3250.0
    assert sig.unit == "rpm"


def test_bus_stats_dto() -> None:
    stats = BusStatsDto(
        timestamp_us=1000000,
        channel=2,
        bus_load_pct=24.5,
        frame_rate_hz=150,
        peak_load_pct=32.0,
        error_frames=0,
        controller_state="active",
    )
    assert stats.bus_load_pct == 24.5
    assert stats.controller_state == "active"


def test_dtc_record_dto() -> None:
    dtc = DtcRecordDto(
        code="P0117",
        description="Engine Coolant Temperature Circuit Low",
        status_byte=0x2F,
        confirmed=True,
        pending=False,
        failed=True,
    )
    assert dtc.code == "P0117"
    assert dtc.confirmed is True
    assert dtc.failed is True


def test_stream_message_envelope() -> None:
    frame = CanFrameDto(
        timestamp_us=1000000,
        channel=2,
        id=0x110,
        dlc=8,
        data_hex="0a0b0c0d0e0f0001",
    )
    msg = StreamMessageDto(type="frame", payload=frame)
    data = msg.model_dump()
    assert data["type"] == "frame"
    assert data["payload"]["id"] == 0x110


def test_synthetic_streamer_generation() -> None:
    streamer = MockCanStreamer()
    batch = streamer.generate_synthetic_batch(timestamp_us=5000000)

    assert isinstance(batch, StreamBatchDto)
    assert len(batch.frames) >= 3
    ids = {f.id for f in batch.frames}
    assert 0x020 in ids  # EkfLean
    assert 0x021 in ids  # VehicleSpeed
    assert 0x110 in ids  # VehicleEngine

    sig_names = {s.name for s in batch.signals}
    assert "LEAN_ANGLE" in sig_names
    assert "ENGINE_SPEED" in sig_names
    assert "VEHICLE_SPEED" in sig_names
    assert "BATTERY_VOLTAGE" in sig_names

    assert batch.stats is not None
    assert batch.stats.bus_load_pct >= 0.0


def test_fixture_streamer_loading() -> None:
    fixtures_dir = Path(__file__).parent / "fixtures" / "v4_vsig_session"
    if not fixtures_dir.is_dir():
        pytest.skip("Fixtures directory not found")

    streamer = MockCanStreamer(fixture_dir=fixtures_dir)
    records = streamer.load_fixture_records()
    assert len(records) > 0
    first_ts, first_frames, first_signals = records[0]
    assert first_ts > 0
    assert len(first_frames) > 0
    assert len(first_signals) > 0


@pytest.mark.anyio
async def test_async_stream_batches() -> None:
    streamer = MockCanStreamer()
    batches: list[StreamBatchDto] = []
    async for batch in streamer.stream_batches(rate_hz=100, max_batches=5, real_time_delay=False):
        batches.append(batch)

    assert len(batches) == 5
    for b in batches:
        assert len(b.frames) > 0
        assert len(b.signals) > 0
        assert b.stats is not None
