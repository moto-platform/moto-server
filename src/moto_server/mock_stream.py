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

from moto_server.schemas.stream import (
    BusStatsDto,
    CanFrameDto,
    DecodedSignalDto,
    StreamBatchDto,
)


class MockCanStreamer:
    """Generates realistic CAN frame streams and decoded physical signals."""

    def __init__(self, fixture_dir: Path | str | None = None) -> None:
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self._alive_counter = 0
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

        # 4-bit monotonic alive counter for E2E
        self._alive_counter = (self._alive_counter + 1) & 0x0F

        frames: list[CanFrameDto] = []
        signals: list[DecodedSignalDto] = []

        # 1. 0x020 EkfLean (8 bytes: Lean Angle, Pitch, Roll Rate, Alive/CRC)
        # Scale: 0.01 deg/LSB, signed 16-bit
        lean_raw = int(lean * 100) & 0xFFFF
        lean_b0 = (lean_raw >> 8) & 0xFF
        lean_b1 = lean_raw & 0xFF
        e2e_byte = (self._alive_counter << 4) & 0xF0
        frames.append(
            CanFrameDto(
                timestamp_us=timestamp_us,
                channel=2,  # Platform CAN
                id=0x020,
                is_extended=False,
                dlc=8,
                data_hex=f"{lean_b0:02x}{lean_b1:02x}00000000{e2e_byte:02x}1d",
                rxtx="rx",
            )
        )
        signals.append(
            DecodedSignalDto(
                name="LEAN_ANGLE",
                value=round(lean, 2),
                unit="deg",
                timestamp_us=timestamp_us,
                raw_value=lean_raw,
                is_valid=True,
            )
        )

        # 2. 0x021 VehicleSpeed (8 bytes: Speed km/h, E2E)
        # Scale: 0.1 km/h/LSB, 16-bit
        speed_raw = int(speed * 10) & 0xFFFF
        sp_b0 = (speed_raw >> 8) & 0xFF
        sp_b1 = speed_raw & 0xFF
        frames.append(
            CanFrameDto(
                timestamp_us=timestamp_us,
                channel=2,
                id=0x021,
                is_extended=False,
                dlc=8,
                data_hex=f"{sp_b0:02x}{sp_b1:02x}00000000{e2e_byte:02x}2a",
                rxtx="rx",
            )
        )
        signals.append(
            DecodedSignalDto(
                name="VEHICLE_SPEED",
                value=round(speed, 1),
                unit="km/h",
                timestamp_us=timestamp_us,
                raw_value=speed_raw,
                is_valid=True,
            )
        )

        # 3. 0x110 VehicleEngine (8 bytes: RPM, Throttle, ECT, Battery)
        # RPM scale: 1 RPM/LSB
        rpm_b0 = (rpm >> 8) & 0xFF
        rpm_b1 = rpm & 0xFF
        tps_b = int(throttle * 2.55) & 0xFF
        ect_b = int(coolant + 40) & 0xFF
        bat_raw = int(battery * 100) & 0xFFFF
        bat_b0 = (bat_raw >> 8) & 0xFF
        bat_b1 = bat_raw & 0xFF
        frames.append(
            CanFrameDto(
                timestamp_us=timestamp_us,
                channel=2,
                id=0x110,
                is_extended=False,
                dlc=8,
                data_hex=f"{rpm_b0:02x}{rpm_b1:02x}{tps_b:02x}{ect_b:02x}{bat_b0:02x}{bat_b1:02x}0000",
                rxtx="rx",
            )
        )
        signals.append(
            DecodedSignalDto(
                name="ENGINE_SPEED",
                value=rpm,
                unit="rpm",
                timestamp_us=timestamp_us,
                raw_value=rpm,
                is_valid=True,
            )
        )
        signals.append(
            DecodedSignalDto(
                name="THROTTLE_POS",
                value=round(throttle, 1),
                unit="%",
                timestamp_us=timestamp_us,
                raw_value=tps_b,
                is_valid=True,
            )
        )
        signals.append(
            DecodedSignalDto(
                name="COOLANT_TEMP",
                value=round(coolant, 1),
                unit="C",
                timestamp_us=timestamp_us,
                raw_value=ect_b,
                is_valid=True,
            )
        )
        signals.append(
            DecodedSignalDto(
                name="BATTERY_VOLTAGE",
                value=round(battery, 2),
                unit="V",
                timestamp_us=timestamp_us,
                raw_value=bat_raw,
                is_valid=True,
            )
        )

        # 4. Periodic Heartbeat 0x081 (HeartbeatRtCore: state, mode, err_cnt) every 5 ticks (10 Hz)
        if self._step % 5 == 0:
            frames.append(
                CanFrameDto(
                    timestamp_us=timestamp_us,
                    channel=2,
                    id=0x081,
                    is_extended=False,
                    dlc=8,
                    data_hex="0100000000000000",
                    rxtx="rx",
                )
            )

        # 5. Vehicle UDS Diagnostics Response (29-bit 0x18DAF110) every 10 ticks (5 Hz)
        if self._step % 10 == 0:
            # UDS 0x62 (positive response to 0x22), DID 0xF40C (Engine RPM)
            frames.append(
                CanFrameDto(
                    timestamp_us=timestamp_us,
                    channel=1,  # Vehicle CAN DLC
                    id=0x18DAF110,
                    is_extended=True,
                    dlc=8,
                    data_hex=f"0462f40c{rpm_b0:02x}{rpm_b1:02x}5555",
                    rxtx="rx",
                )
            )

        # Bus load calculation:
        # Classic CAN frame ~110-128 bits with stuffing. At 500 kbps, 1 frame ≈ 0.23 ms
        num_frames = len(frames)
        est_bits = num_frames * 120
        bus_load_pct = min(100.0, round((est_bits / (500000 * 0.02)) * 100, 1))
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
                            id=0x18DAF110,
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
                            unit="rpm",
                            timestamp_us=ts_us,
                        )
                    )
                if row.get("speed_kmh"):
                    signals.append(
                        DecodedSignalDto(
                            name="VEHICLE_SPEED",
                            value=float(row["speed_kmh"]),
                            unit="km/h",
                            timestamp_us=ts_us,
                        )
                    )
                if row.get("coolant_c"):
                    signals.append(
                        DecodedSignalDto(
                            name="COOLANT_TEMP",
                            value=float(row["coolant_c"]),
                            unit="C",
                            timestamp_us=ts_us,
                        )
                    )
                if row.get("battery_v"):
                    signals.append(
                        DecodedSignalDto(
                            name="BATTERY_VOLTAGE",
                            value=float(row["battery_v"]),
                            unit="V",
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
