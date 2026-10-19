"""Stream schema package."""

from __future__ import annotations

from moto_server.schemas.stream import (
    BusStatsDto,
    CanFrameDto,
    DecodedSignalDto,
    DtcRecordDto,
    StreamBatchDto,
    StreamMessageDto,
)

__all__ = [
    "BusStatsDto",
    "CanFrameDto",
    "DecodedSignalDto",
    "DtcRecordDto",
    "StreamBatchDto",
    "StreamMessageDto",
]
