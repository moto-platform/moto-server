"""MDF4 export of a stored session.

MDF4 is the archive/sharing format (phase0 plan section 3.3) next to the Parquet
outputs: ``<session_dir>/parquet/<session_dir.name>.mf4``. Parquet remains the
ML source; MDF4 is for cross-tool exchange. Needs the optional ``mdf4`` extra
(asammdf, LGPL-3.0+, kept out of the default install and the image, D-091).

Channel groups:

* telemetry: every numeric ``telemetry.parquet`` column on the node device time
  (``rx_mono_ms`` for old v2 sessions that predate device time);
* imu: every numeric ``imu.parquet`` column on the node device time;
* gps: speed/heading and the other numeric ``gps.parquet`` columns on node time;
* vehicle signals: one group per catalog signal in ``signals.parquet``, valid
  records only, timestamped by ``sample_device_time_ms`` (D-066).

Units come from ``signals.py`` and the generated defs tables: physical signals,
ages, tester durations, IMU scale and GPS scale. Unitless counts/flags keep an
empty unit; unknown columns also keep an empty unit and are logged.

asammdf timestamps are seconds. The parquet times are milliseconds, so the
export divides by 1000 and the time axis stays physically correct.

Position: latitude, longitude, altitude/height and other position-looking
columns are not written unless ``include_position=True`` (D-076, invariant 7).
The filter is ``position.is_position_name``, shared with the read API.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from asammdf import MDF, Signal

from vehicle_server import ble_schema, signals
from vehicle_server.decode import CAN_HEALTH_FIELDS, csv_column_for_field
from vehicle_server.position import is_position_name

logger = logging.getLogger(__name__)

MDF_VERSION = "4.10"
_VEHICLE_SIGNAL_GROUP = "vehicle signals"
_METADATA_COLUMNS = frozenset(
    {
        "session_id",
        "rx_utc",
        "rx_mono_ms",
        "device_time_ms",
        "decode_error",
        "fix_type_name",
    }
)


@dataclass
class _GroupResult:
    channels: int = 0
    unsourced_units: set[str] = field(default_factory=set)
    excluded_positions: set[str] = field(default_factory=set)


def _is_numeric(data_type: pa.DataType) -> bool:
    return (
        pa.types.is_integer(data_type)
        or pa.types.is_floating(data_type)
        or pa.types.is_boolean(data_type)
    )


def _known_units(schema: dict[str, Any]) -> dict[str, str]:
    units: dict[str, str] = {}
    for defs_signal in signals.known_defs_signals(schema):
        units[signals.physical_column(defs_signal)] = signals.physical_unit(defs_signal)
        units[signals.age_column(defs_signal)] = schema["age"]["unit"]
        units[signals.valid_column(defs_signal)] = ""
    for schema_field in schema["fields"]:
        if schema_field["name"] == "deviceTimeMs" or "ageOf" in schema_field:
            continue
        if schema_field["name"].endswith("Ms"):
            units[csv_column_for_field(schema_field["name"])] = schema["deviceTime"]["unit"]
    for column in (
        "seq",
        "packet_version",
        "step_gap_over_count",
        "rtt_did",
        "rtt_count",
        "rtt_nrc78_count",
    ):
        units[column] = ""
    for column in (csv_column_for_field(name) for name in CAN_HEALTH_FIELDS):
        units[column] = ""
    return units


def _imu_units(schema: dict[str, Any]) -> dict[str, str]:
    accel = schema["imuBlock"]["scale"]["accel"]["unit"]
    gyro = schema["imuBlock"]["scale"]["gyro"]["unit"]
    units = {f"a{axis}_g": accel for axis in "xyz"}
    units.update({f"g{axis}_dps": gyro for axis in "xyz"})
    for column in (
        "block_seq",
        "block_flags",
        "block_missing_samples",
        "sample_index",
        "ax_raw",
        "ay_raw",
        "az_raw",
        "gx_raw",
        "gy_raw",
        "gz_raw",
    ):
        units[column] = ""
    return units


def _gps_units(schema: dict[str, Any]) -> dict[str, str]:
    speed = schema["gpsBlock"]["scale"]["speed"]["unit"]
    heading = schema["gpsBlock"]["scale"]["heading"]["unit"]
    units = {
        "ground_speed_mps": speed,
        "speed_accuracy_mps": speed,
        "heading_of_motion_deg": heading,
        "heading_accuracy_deg": heading,
    }
    for column in (
        "ground_speed",
        "speed_accuracy",
        "heading_of_motion",
        "heading_accuracy",
        "fix_type",
        "num_sv",
        "flags",
        "gnss_fix_ok",
        "parse_error",
        "uart_overflow",
    ):
        units[column] = ""
    return units


def _time_column(table: pa.Table, candidates: tuple[str, ...]) -> str | None:
    present = [name for name in candidates if name in table.column_names]
    for name in present:
        if table[name].null_count == 0:
            return name
    return present[0] if present else None


def _samples(values: list[Any]) -> np.ndarray:
    if any(value is None for value in values):
        return np.array([np.nan if value is None else value for value in values], dtype=np.float64)
    return np.array(values)


def _append_wide_group(
    mdf: MDF,
    table: pa.Table,
    *,
    acq_name: str,
    time_candidates: tuple[str, ...],
    units: dict[str, str],
    include_position: bool,
    skip: frozenset[str] = frozenset(),
) -> _GroupResult:
    result = _GroupResult()
    time_column = _time_column(table, time_candidates)
    if time_column is None:
        return result

    raw_times = table[time_column].to_pylist()
    keep = [index for index, value in enumerate(raw_times) if value is not None]
    if not keep:
        return result
    times = np.array([raw_times[index] for index in keep], dtype=np.float64) / 1000.0
    order = np.argsort(times, kind="stable")
    times = times[order]

    channel_names = []
    for name in table.column_names:
        if name == time_column or name in _METADATA_COLUMNS or name in skip:
            continue
        if not _is_numeric(table.schema.field(name).type):
            continue
        if not include_position and is_position_name(name):
            result.excluded_positions.add(name)
            continue
        channel_names.append(name)

    channel_signals = []
    for name in channel_names:
        raw_values = table[name].to_pylist()
        samples = _samples([raw_values[index] for index in keep])[order]
        if name not in units:
            result.unsourced_units.add(name)
        channel_signals.append(Signal(samples, times, name=name, unit=units.get(name, "")))
    if not channel_signals:
        return result
    mdf.append(channel_signals, acq_name=acq_name)
    result.channels = len(channel_signals)
    return result


def _append_vehicle_signals(mdf: MDF, table: pa.Table, *, include_position: bool) -> _GroupResult:
    required = {"signal", "unit", "value", "sample_device_time_ms", "state_name"}
    missing = required - set(table.column_names)
    if missing:
        raise ValueError(f"signals.parquet is missing required columns: {sorted(missing)}")

    result = _GroupResult()
    by_signal: dict[str, dict[int, tuple[float, str | None]]] = {}
    for signal, unit, value, time_ms, state in zip(
        table["signal"].to_pylist(),
        table["unit"].to_pylist(),
        table["value"].to_pylist(),
        table["sample_device_time_ms"].to_pylist(),
        table["state_name"].to_pylist(),
        strict=True,
    ):
        if signal is None or state != "valid" or value is None or time_ms is None:
            continue
        if not math.isfinite(float(value)):
            continue
        if is_position_name(signal):
            if not include_position:
                result.excluded_positions.add(signal)
                continue
        by_signal.setdefault(signal, {})[int(time_ms)] = (float(value), unit)

    for signal in sorted(by_signal):
        rows = sorted(by_signal[signal].items())
        times = np.array([time_ms for time_ms, _ in rows], dtype=np.float64) / 1000.0
        samples = np.array([value for _, (value, _) in rows], dtype=np.float64)
        row_units = {unit for _, (_, unit) in rows if unit}
        if len(row_units) == 1:
            channel_unit = row_units.pop()
        else:
            if row_units:
                result.unsourced_units.add(f"{signal} (multiple units)")
            channel_unit = ""
        if not channel_unit:
            result.unsourced_units.add(signal)
        mdf.append(
            [Signal(samples, times, name=signal, unit=channel_unit)],
            acq_name=_VEHICLE_SIGNAL_GROUP,
        )
        result.channels += 1
    return result


def _read_table(parquet_dir: Path, name: str) -> pa.Table | None:
    path = parquet_dir / name
    return pq.read_table(path) if path.is_file() else None


def export_mdf4(session_dir: Path, *, include_position: bool = False) -> Path:
    """Write one MDF4 file next to a session's parquet outputs and return its path.

    Missing parquet files skip their group. A session with no exportable data raises
    ``ValueError``. Position columns are omitted unless ``include_position`` is true.
    """
    parquet_dir = session_dir / "parquet"
    if not parquet_dir.is_dir():
        raise ValueError(f"session has no parquet directory: {parquet_dir}")

    schema = ble_schema.load_default_schema()
    telemetry = _read_table(parquet_dir, "telemetry.parquet")
    imu = _read_table(parquet_dir, "imu.parquet")
    gps = _read_table(parquet_dir, "gps.parquet")
    vehicle = _read_table(parquet_dir, "signals.parquet")
    if telemetry is None and imu is None and gps is None and vehicle is None:
        raise ValueError(f"session has no MDF4 data: {parquet_dir}")

    results: list[_GroupResult] = []
    mdf = MDF(version=MDF_VERSION)
    try:
        if telemetry is not None:
            results.append(
                _append_wide_group(
                    mdf,
                    telemetry,
                    acq_name="telemetry",
                    time_candidates=("device_time_ms", "rx_mono_ms"),
                    units=_known_units(schema),
                    include_position=include_position,
                )
            )
        if imu is not None:
            results.append(
                _append_wide_group(
                    mdf,
                    imu,
                    acq_name="imu",
                    time_candidates=("device_time_ms", "rx_mono_ms"),
                    units=_imu_units(schema),
                    include_position=include_position,
                )
            )
        if gps is not None:
            results.append(
                _append_wide_group(
                    mdf,
                    gps,
                    acq_name="gps",
                    time_candidates=("device_time_ms", "rx_mono_ms"),
                    units=_gps_units(schema),
                    include_position=include_position,
                    skip=frozenset({"seq"}),
                )
            )
        if vehicle is not None:
            results.append(_append_vehicle_signals(mdf, vehicle, include_position=include_position))

        channels = sum(result.channels for result in results)
        if channels == 0:
            raise ValueError(f"session has no MDF4 data: {parquet_dir}")

        unsourced = sorted({name for result in results for name in result.unsourced_units})
        if unsourced:
            logger.warning("MDF4 export has no defs unit for: %s", ", ".join(unsourced))
        excluded = sorted({name for result in results for name in result.excluded_positions})
        if excluded:
            logger.info("MDF4 export excluded position columns: %s", ", ".join(excluded))

        output = parquet_dir / f"{session_dir.name}.mf4"
        mdf.save(output, overwrite=True)
    finally:
        mdf.close()
    return output
