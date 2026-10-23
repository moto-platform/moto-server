"""Virtual CAN & Telemetry Stream Generator for development and testing.

Produces realistic CAN frames (Platform CAN 11-bit IDs and Vehicle UDS 29-bit frames)
and physical decoded signals without requiring physical hardware.
Can stream from recorded test fixtures or synthesize realistic riding dynamics.
"""

from __future__ import annotations

import asyncio
import csv
import math
import time
from collections.abc import AsyncIterator
from pathlib import Path

from moto_server.can_codec import Signal, e2e, encode, platform_messages, unpack_raw
from moto_server.defs import vehicle_cl250
from moto_server.schemas.stream import (
    BusStatsDto,
    CanFrameDto,
    DecodedSignalDto,
    StreamBatchDto,
)

# Engine signals that carry a matching <NAME>_VALID bit in VehicleEngine.
_ENGINE_SIGNALS = ("ENGINE_SPEED", "BATTERY_VOLTAGE", "COOLANT_TEMP", "THROTTLE_POS")


def _usable_states(signal: Signal) -> set[int]:
    """Enum values of a *_STATE / *_VALID signal that mean the value is usable."""
    return {signal.choices[n] for n in ("ESTIMATED", "CLAMPED", "VALID") if n in signal.choices}


def uds_read_response(did_name: str, physical: float) -> bytes:
    """ISO-TP single frame carrying a positive ReadDataByIdentifier (0x62) response."""
    did, length, num, den, offset, *_ = vehicle_cl250.DIDS[did_name]
    raw = max(0, min((1 << (8 * length)) - 1, round((physical - offset) * den / num)))
    body = bytes([0x62, did >> 8, did & 0xFF]) + raw.to_bytes(length, "big")
    frame = bytes([len(body)]) + body
    return frame + bytes([vehicle_cl250.PADDING_BYTE]) * (8 - len(frame))


class MockCanStreamer:
    """Generates realistic CAN frame streams and decoded physical signals."""

    def __init__(self, fixture_dir: Path | str | None = None) -> None:
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self._messages = platform_messages()
        self._tx = {name: e2e.TxState() for name in self._messages}
        self._step = 0
        self._peak_bus_load = 0.0

    def generate_synthetic_batch(self, timestamp_us: int) -> StreamBatchDto:
        """Synthesize a realistic batch of frames, signals, and bus stats."""
        self._step += 1
        t_sec = (self._step * 20) / 1000.0  # 50 Hz nominal tick (20 ms)

        # Dynamic simulated telemetry:
        # RPM oscillates between 1500 (idle) and 6500 (cruising / revving)
        rpm = 3000 + int(1800 * math.sin(t_sec * 0.4) + 500 * math.sin(t_sec * 1.5))
        rpm = max(1200, min(8000, rpm))

        # Speed follows RPM smoothly (30 to 85 km/h)
        speed = 45.0 + 25.0 * math.sin(t_sec * 0.3)
        speed = max(0.0, speed)

        # Lean angle oscillates between -35 deg (left turn) and +35 deg (right turn)
        lean = 22.0 * math.sin(t_sec * 0.2)
        throttle = max(0.0, min(100.0, 30.0 + 20.0 * math.sin(t_sec * 0.5)))
        coolant = 82.0 + 4.0 * math.sin(t_sec * 0.05)
        battery = 14.1 + 0.15 * math.sin(t_sec * 0.8)

        frames: list[CanFrameDto] = []
        signals: list[DecodedSignalDto] = []

        def emit(
            name: str,
            physical: dict[str, float],
            states: dict[str, str],
            valid: dict[str, str],
        ) -> None:
            """Encode a platform message from moto_defs, then report what the frame decodes to.

            ``valid`` maps each reported signal to the VALID bit / STATE field that qualifies it.
            """
            msg = self._messages[name]
            data = encode(msg, physical, states, self._tx[name])
            frames.append(
                CanFrameDto(
                    timestamp_us=timestamp_us,
                    channel=2,  # Platform CAN
                    id=msg.frame_id,
                    is_extended=False,
                    dlc=msg.length,
                    data_hex=data.hex(),
                    rxtx="rx",
                )
            )
            raws = unpack_raw(msg, data)
            for sig_name, qualifier in valid.items():
                sig = msg.signals[sig_name]
                is_valid = raws[qualifier] in _usable_states(msg.signals[qualifier])
                signals.append(
                    DecodedSignalDto(
                        name=sig_name,
                        value=sig.to_physical(raws[sig_name]),
                        unit=sig.unit,
                        timestamp_us=timestamp_us,
                        raw_value=raws[sig_name],
                        is_valid=is_valid,
                    )
                )

        # 0x020 EkfLean: E2E, LEAN_ANGLE with its state and quality
        emit(
            "EkfLean",
            {"LEAN_ANGLE": lean, "LEAN_ANGLE_QUALITY": 100},
            {"LEAN_ANGLE_STATE": "ESTIMATED"},
            {"LEAN_ANGLE": "LEAN_ANGLE_STATE"},
        )
        # 0x021 VehicleSpeed
        emit(
            "VehicleSpeed",
            {"VEHICLE_SPEED": speed, "VEHICLE_SPEED_VALID": 1},
            {},
            {"VEHICLE_SPEED": "VEHICLE_SPEED_VALID"},
        )
        # 0x110 VehicleEngine (not E2E protected)
        engine_valid = {n: f"{n}_VALID" for n in _ENGINE_SIGNALS}
        emit(
            "VehicleEngine",
            {
                "ENGINE_SPEED": rpm,
                "THROTTLE_POS": throttle,
                "COOLANT_TEMP": coolant,
                "BATTERY_VOLTAGE": battery,
                **{f"{n}_VALID": 1 for n in _ENGINE_SIGNALS},
                "ECU_PRESENT": 1,
            },
            {},
            engine_valid,
        )
        # Periodic rt-core heartbeat every 5 ticks (10 Hz is the nominal heartbeat rate)
        if self._step % 5 == 0:
            hb = self._messages["HeartbeatRtCore"]
            hb_data = encode(
                hb,
                {"ERROR_COUNT": 0, "UPTIME": int(t_sec)},
                {"NODE_MODE": "NORMAL"},
                self._tx["HeartbeatRtCore"],
            )
            frames.append(
                CanFrameDto(
                    timestamp_us=timestamp_us,
                    channel=2,
                    id=hb.frame_id,
                    is_extended=False,
                    dlc=hb.length,
                    data_hex=hb_data.hex(),
                    rxtx="rx",
                )
            )

        # Vehicle UDS response (ISO-TP single frame, positive response 0x62) every 10 ticks
        if self._step % 10 == 0:
            frames.append(
                CanFrameDto(
                    timestamp_us=timestamp_us,
                    channel=1,  # Vehicle CAN
                    id=vehicle_cl250.RESPONSE_ID,
                    is_extended=True,
                    dlc=8,
                    data_hex=uds_read_response("ENGINE_SPEED", rpm).hex(),
                    rxtx="rx",
                )
            )

        # Bus load calculation:
        # Classic CAN frame ~110-128 bits with stuffing (~120 bits assumed) at the defs bitrate
        num_frames = len(frames)
        est_bits = num_frames * 120
        bus_load_pct = min(100.0, round((est_bits / (vehicle_cl250.BITRATE * 0.02)) * 100, 1))
        self._peak_bus_load = max(self._peak_bus_load, bus_load_pct)

        stats = BusStatsDto(
            timestamp_us=timestamp_us,
            channel=2,
            bus_load_pct=bus_load_pct,
            frame_rate_hz=num_frames * 50,
            peak_load_pct=self._peak_bus_load,
            error_frames=0,
            controller_state="active",
        )

        return StreamBatchDto(
            timestamp_us=timestamp_us,
            frames=frames,
            signals=signals,
            stats=stats,
        )

    def load_fixture_records(self) -> list[tuple[int, list[CanFrameDto], list[DecodedSignalDto]]]:
        """Load and parse records from fixture telemetry.csv and signals.csv if available."""
        if not self.fixture_dir or not self.fixture_dir.is_dir():
            return []

        telem_file = self.fixture_dir / "telemetry.csv"
        if not telem_file.is_file():
            return []

        batches: list[tuple[int, list[CanFrameDto], list[DecodedSignalDto]]] = []
        with telem_file.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts_ms = int(row.get("rx_mono_ms") or row.get("device_time_ms") or 0)
                ts_us = ts_ms * 1000
                raw_hex = (row.get("raw_hex") or "").strip()

                frames: list[CanFrameDto] = []
                if raw_hex:
                    frames.append(
                        CanFrameDto(
                            timestamp_us=ts_us,
                            channel=1,
                            id=vehicle_cl250.RESPONSE_ID,
                            is_extended=True,
                            dlc=min(64, len(raw_hex) // 2),
                            data_hex=raw_hex[:16],  # first 8 bytes
                            rxtx="rx",
                        )
                    )

                signals: list[DecodedSignalDto] = []
                if row.get("rpm"):
                    signals.append(
                        DecodedSignalDto(
                            name="ENGINE_SPEED",
                            value=float(row["rpm"]),
                            unit=vehicle_cl250.DIDS["ENGINE_SPEED"][5],
                            timestamp_us=ts_us,
                        )
                    )
                if row.get("speed_kmh"):
                    signals.append(
                        DecodedSignalDto(
                            name="VEHICLE_SPEED",
                            value=float(row["speed_kmh"]),
                            unit=vehicle_cl250.DIDS["VEHICLE_SPEED"][5],
                            timestamp_us=ts_us,
                        )
                    )
                if row.get("coolant_c"):
                    signals.append(
                        DecodedSignalDto(
                            name="COOLANT_TEMP",
                            value=float(row["coolant_c"]),
                            unit=vehicle_cl250.DIDS["COOLANT_TEMP"][5],
                            timestamp_us=ts_us,
                        )
                    )
                if row.get("battery_v"):
                    signals.append(
                        DecodedSignalDto(
                            name="BATTERY_VOLTAGE",
                            value=float(row["battery_v"]),
                            unit=vehicle_cl250.DIDS["BATTERY_VOLTAGE"][5],
                            timestamp_us=ts_us,
                        )
                    )

                batches.append((ts_us, frames, signals))

        return batches

    async def stream_batches(
        self,
        rate_hz: int = 50,
        max_batches: int | None = None,
        real_time_delay: bool = True,
    ) -> AsyncIterator[StreamBatchDto]:
        """Asynchronously stream batches at the requested frequency."""
        fixture_records = self.load_fixture_records()
        interval = 1.0 / rate_hz
        batch_count = 0

        if fixture_records:
            idx = 0
            while True:
                ts_us, frames, signals = fixture_records[idx]
                stats = BusStatsDto(
                    timestamp_us=ts_us,
                    channel=1,
                    bus_load_pct=18.5,
                    frame_rate_hz=rate_hz * len(frames),
                    peak_load_pct=25.0,
                    error_frames=0,
                    controller_state="active",
                )
                yield StreamBatchDto(
                    timestamp_us=ts_us,
                    frames=frames,
                    signals=signals,
                    stats=stats,
                )
                batch_count += 1
                if max_batches and batch_count >= max_batches:
                    break
                idx = (idx + 1) % len(fixture_records)
                if real_time_delay:
                    await asyncio.sleep(interval)
        else:
            while True:
                now_us = int(time.time() * 1_000_000)
                batch = self.generate_synthetic_batch(now_us)
                yield batch
                batch_count += 1
                if max_batches and batch_count >= max_batches:
                    break
                if real_time_delay:
                    await asyncio.sleep(interval)


async def main() -> None:
    """CLI runner demonstrating mock stream output."""
    streamer = MockCanStreamer()
    print("Starting Mock CAN Stream (50 Hz). Press Ctrl+C to stop.")
    count = 0
    async for batch in streamer.stream_batches(rate_hz=50, max_batches=20, real_time_delay=False):
        count += 1
        print(
            f"Batch {count:03d} | Frames: {len(batch.frames)} | Signals: {len(batch.signals)} | "
            f"Load: {batch.stats.bus_load_pct if batch.stats else 0}% | "
            f"Signals: {[s.name + '=' + str(s.value) for s in batch.signals[:3]]}"
        )


if __name__ == "__main__":
    asyncio.run(main())
