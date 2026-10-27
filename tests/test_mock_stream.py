"""Unit tests for schemas.stream and mock_stream module."""

from __future__ import annotations

from pathlib import Path

import pytest

from vehicle_server.can_codec import e2e, platform_messages, unpack_raw
from vehicle_server.defs import vehicle_cl250
from vehicle_server.mock_stream import MockCanStreamer, uds_read_response
from vehicle_server.schemas.stream import (
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


def _run(n: int) -> list[StreamBatchDto]:
    streamer = MockCanStreamer()
    return [streamer.generate_synthetic_batch(timestamp_us=i * 20_000) for i in range(1, n + 1)]


def test_round_trip_through_defs_tables() -> None:
    msgs = platform_messages()
    by_name = {m.frame_id: m for m in msgs.values()}
    for batch in _run(40):
        sigs = {s.name: s for s in batch.signals}
        for frame in batch.frames:
            if frame.is_extended:
                continue
            msg = by_name[frame.id]
            raws = unpack_raw(msg, bytes(frame.bytes_list()))
            for name, sig in sigs.items():
                if name in msg.signals:
                    decoded = msg.signals[name].to_physical(raws[name])
                    assert decoded == pytest.approx(sig.value)
                    assert sig.unit == msg.signals[name].unit
                    assert sig.raw_value == raws[name]


def test_e2e_crc_and_counter_follow_defs_profile() -> None:
    msgs = platform_messages()
    by_id = {m.frame_id: m for m in msgs.values()}
    counters: dict[int, list[int]] = {}
    for batch in _run(30):
        for frame in batch.frames:
            msg = by_id.get(frame.id) if not frame.is_extended else None
            if msg is None or msg.e2e_data_id is None:
                continue
            data = bytes(frame.bytes_list())
            assert data[e2e.CRC_OFFSET] == e2e.compute_crc(msg.e2e_data_id, data)
            counters.setdefault(frame.id, []).append(data[e2e.COUNTER_OFFSET] & e2e.COUNTER_MASK)
    assert {0x020, 0x021, 0x081} <= set(counters)
    for seq in counters.values():
        assert all((b - a) & e2e.COUNTER_MASK == 1 for a, b in zip(seq, seq[1:], strict=False))


def test_e2e_receiver_accepts_stream() -> None:
    msgs = platform_messages()
    msg = msgs["EkfLean"]
    rx = e2e.RxState()
    statuses = []
    for i, batch in enumerate(_run(10)):
        frame = next(f for f in batch.frames if f.id == msg.frame_id)
        statuses.append(
            e2e.check(msg.e2e_data_id, 1, 60, bytes(frame.bytes_list()), rx, now_ms=i * 20)
        )
    assert statuses[0] == e2e.E2EStatus.INITIAL
    assert all(s == e2e.E2EStatus.OK for s in statuses[1:])


def test_valid_bits_and_states_match_reported_validity() -> None:
    msgs = platform_messages()
    batch = _run(1)[0]
    frames = {f.id: bytes(f.bytes_list()) for f in batch.frames}
    eng = msgs["VehicleEngine"]
    raws = unpack_raw(eng, frames[eng.frame_id])
    for name in ("ENGINE_SPEED", "BATTERY_VOLTAGE", "COOLANT_TEMP", "THROTTLE_POS"):
        assert raws[f"{name}_VALID"] == 1
    assert raws["ECU_PRESENT"] == 1
    lean = msgs["EkfLean"]
    lraws = unpack_raw(lean, frames[lean.frame_id])
    assert lraws["LEAN_ANGLE_STATE"] == lean.signals["LEAN_ANGLE_STATE"].choices["ESTIMATED"]
    assert lraws["LEAN_ANGLE_QUALITY"] > 0
    assert all(s.is_valid for s in batch.signals)


def test_known_scales() -> None:
    msgs = platform_messages()
    batch = _run(1)[0]
    frames = {f.id: bytes(f.bytes_list()) for f in batch.frames}
    sigs = {s.name: s for s in batch.signals}
    eng = unpack_raw(msgs["VehicleEngine"], frames[0x110])
    assert eng["ENGINE_SPEED"] == round(sigs["ENGINE_SPEED"].value * 4)
    assert eng["BATTERY_VOLTAGE"] == round(sigs["BATTERY_VOLTAGE"].value * 1000)
    spd = unpack_raw(msgs["VehicleSpeed"], frames[0x021])
    assert spd["VEHICLE_SPEED"] == round(sigs["VEHICLE_SPEED"].value * 100)
    assert sigs["COOLANT_TEMP"].unit == "degC"


def test_heartbeat_uses_encoder_fields() -> None:
    msgs = platform_messages()
    hb = msgs["HeartbeatRtCore"]
    frame = next(f for b in _run(5) for f in b.frames if f.id == hb.frame_id)
    raws = unpack_raw(hb, bytes(frame.bytes_list()))
    assert raws["NODE_MODE"] == hb.signals["NODE_MODE"].choices["NORMAL"]
    assert raws["ERROR_COUNT"] == 0


def test_uds_response_uses_defs_did_scaling() -> None:
    did, length, num, den, offset, *_ = vehicle_cl250.DIDS["ENGINE_SPEED"]
    frame = next(f for b in _run(10) for f in b.frames if f.is_extended)
    assert frame.id == vehicle_cl250.RESPONSE_ID
    data = bytes(frame.bytes_list())
    assert data[0] == 3 + length  # ISO-TP single-frame length
    assert data[1] == 0x62
    assert int.from_bytes(data[2:4], "big") == did
    raw = int.from_bytes(data[4 : 4 + length], "big")
    rpm = raw * num / den + offset
    assert 1200 <= rpm <= 8000  # the stream's rpm range, not the raw (x4) value
    assert uds_read_response("ENGINE_SPEED", 3291.25)[4:6] == (3291 * 4 + 1).to_bytes(2, "big")
    assert data[4 + length :] == bytes([vehicle_cl250.PADDING_BYTE]) * (4 - length)


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
