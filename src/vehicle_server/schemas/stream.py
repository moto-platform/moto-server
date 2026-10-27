"""Data Transfer Objects (DTOs) for CAN Analyzer streaming & real-time telemetry."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CanFrameDto(BaseModel):
    """Raw CAN frame representation."""

    timestamp_us: int = Field(..., description="Timestamp in microseconds")
    channel: int = Field(default=1, description="Bus channel index (1: Vehicle, 2: Platform)")
    id: int = Field(..., description="CAN identifier (11-bit standard or 29-bit extended)")
    is_extended: bool = Field(default=False, description="True if 29-bit extended ID")
    dlc: int = Field(..., ge=0, le=64, description="Data length code")
    data_hex: str = Field(..., description="Payload data encoded as lowercase hexadecimal string")
    rxtx: Literal["rx", "tx"] = Field(default="rx", description="Frame direction")

    def bytes_list(self) -> list[int]:
        """Return the payload as a list of integer byte values (0-255)."""
        clean = self.data_hex.strip()
        if not clean:
            return []
        return [int(clean[i : i + 2], 16) for i in range(0, len(clean), 2)]


class DecodedSignalDto(BaseModel):
    """Physical scaled signal derived from a CAN frame or DID."""

    name: str = Field(..., description="Signal name matching vehicle-defs catalog")
    value: float | int | str = Field(..., description="Physical scaled value")
    unit: str = Field(default="", description="Engineering unit (e.g. rpm, km/h, deg, V, C)")
    timestamp_us: int = Field(..., description="Timestamp in microseconds")
    raw_value: int | None = Field(default=None, description="Raw unscaled integer value")
    is_valid: bool = Field(default=True, description="False if sensor error, timeout, or invalid")


class BusStatsDto(BaseModel):
    """Bus performance, utilization, and health metrics."""

    timestamp_us: int = Field(..., description="Timestamp in microseconds")
    channel: int = Field(default=1, description="Bus channel index")
    bus_load_pct: float = Field(..., ge=0.0, le=100.0, description="Bus load percentage")
    frame_rate_hz: int = Field(..., ge=0, description="Frames received per second")
    peak_load_pct: float = Field(..., ge=0.0, le=100.0, description="Peak bus load in session")
    error_frames: int = Field(default=0, ge=0, description="Cumulative error frame counter")
    controller_state: Literal["active", "warning", "passive", "bus_off"] = Field(
        default="active", description="CAN controller state machine status"
    )


class DtcRecordDto(BaseModel):
    """Diagnostic Trouble Code (DTC) representation under ISO 14229-1."""

    code: str = Field(..., description="DTC string (e.g. P0117, P0335, U0001)")
    description: str = Field(default="", description="Human-readable fault description")
    status_byte: int = Field(..., ge=0, le=255, description="ISO 14229-1 status byte")
    confirmed: bool = Field(default=False, description="Bit 3 confirmedDTC")
    pending: bool = Field(default=False, description="Bit 2 pendingDTC")
    failed: bool = Field(default=False, description="Bit 0 testFailed")
    snapshot_data: dict[str, Any] | None = Field(
        default=None, description="D-080 freeze frame snapshot records"
    )


class StreamBatchDto(BaseModel):
    """High-throughput multi-frame batch container to minimize WebSocket framing overhead."""

    timestamp_us: int = Field(..., description="Batch emission timestamp")
    frames: list[CanFrameDto] = Field(default_factory=list)
    signals: list[DecodedSignalDto] = Field(default_factory=list)
    stats: BusStatsDto | None = Field(default=None)


class StreamMessageDto(BaseModel):
    """Top-level message envelope for WebSocket communication."""

    type: Literal["frame", "signal", "stats", "dtc", "batch", "heartbeat"]
    payload: (
        CanFrameDto
        | DecodedSignalDto
        | BusStatsDto
        | DtcRecordDto
        | StreamBatchDto
        | dict[str, Any]
    )
