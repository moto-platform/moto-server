"""Downsampled time series of a stored session (GET /sessions/{id}/series).

Series names (the same prefixes as vehicle_server.derived.inputs):
    did.<CATALOG_NAME>   a vehicle_catalog DID with at least one sample
    sensor.<name>        a vehicle_derived.SENSORS entry that SessionInputs resolves to a
                         non-empty series (never a GPS position, D-060)
    derived.<signal>     rows of parquet/derived.parquet with valid == true

Units come from the defs (catalog / SENSORS) or from the unit column of derived.parquet.

Time axis: the node's device_time_ms (uint32 wrap removed by SessionInputs). t0 is the
smallest first-sample time over ALL available series of the session, so two calls with
different filters or windows share one axis; t_ms = sample time - t0.

Downsampling is min/max bucketing: the window is cut into equal-count buckets and the
minimum and maximum sample of each bucket are kept in time order. A spike therefore always
survives, which a plain decimation would lose (anomaly work reads these series).
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

from vehicle_server.defs import vehicle_catalog, vehicle_derived
from vehicle_server.derived import derived_path
from vehicle_server.derived.inputs import SessionInputs

DEFAULT_MAX_POINTS = 2000
MIN_MAX_POINTS = 10
MAX_MAX_POINTS = 20000

_HORIZONTAL_POSITION_TOKENS = frozenset({"lat", "lon", "lng", "latitude", "longitude"})
_VERTICAL_POSITION_TOKENS = frozenset({"height", "alt", "altitude"})
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class SeriesData:
    """One full-resolution series: absolute device times (ascending) and values."""

    name: str
    kind: str  # "did" | "sensor" | "derived"
    unit: str | None
    times: list[int]
    values: list[float]


def downsample_minmax(
    times: list[int], values: list[float], max_points: int
) -> tuple[list[int], list[float]]:
    """Min/max bucket reduction to at most max_points samples, in time order.

    The input is split into max_points // 2 equal-count buckets (floor, so the output can
    never exceed max_points) and the first minimum and first maximum of each bucket are
    emitted, once if they coincide. At or below max_points the input is returned as is.
    """
    n = len(times)
    if n <= max_points:
        return list(times), list(values)
    buckets = max(max_points // 2, 1)
    keep: list[int] = []
    for b in range(buckets):
        lo, hi = b * n // buckets, (b + 1) * n // buckets
        if lo == hi:
            continue
        chunk = values[lo:hi]
        i_min = lo + chunk.index(min(chunk))
        i_max = lo + chunk.index(max(chunk))
        keep.extend(sorted({i_min, i_max}))
    return [times[i] for i in keep], [values[i] for i in keep]


def window(
    times: list[int], values: list[float], lo: int | None, hi: int | None
) -> tuple[list[int], list[float]]:
    """Samples with lo <= time <= hi (absolute times; None leaves a side open)."""
    start = 0 if lo is None else bisect.bisect_left(times, lo)
    stop = len(times) if hi is None else bisect.bisect_right(times, hi)
    return times[start:stop], values[start:stop]


def is_position_name(name: str) -> bool:
    """Whether a series name looks like a GPS position (D-076, invariant 7).

    A horizontal component is refused anywhere in the name; a vertical one only as the
    last token, so a relative quantity such as ``derived.altitude_change`` stays.
    """
    normalized = _CAMEL_BOUNDARY.sub("_", name).lower()
    tokens = [token for token in _TOKEN_SPLIT.split(normalized) if token]
    if any(token in _HORIZONTAL_POSITION_TOKENS for token in tokens):
        return True
    return bool(tokens) and tokens[-1] in _VERTICAL_POSITION_TOKENS


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear-interpolation percentile (the numpy default method)."""
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _stats(values: list[float]) -> dict[str, float]:
    count = len(values)
    mean = math.fsum(values) / count
    ordered = sorted(values)
    return {
        "count": count,
        "mean": mean,
        "std": math.sqrt(math.fsum((value - mean) ** 2 for value in values) / count),
        "min": ordered[0],
        "max": ordered[-1],
        "p05": _percentile(ordered, 0.05),
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
    }


def build_stats_response(
    session_id: str,
    all_series: dict[str, SeriesData],
    names: list[str] | None,
    start_ms: int | None,
    end_ms: int | None,
) -> dict:
    """Exact statistics over the full-resolution points in the requested window.

    ``start_ms``/``end_ms`` are relative to ``t0_device_ms`` (the same axis as
    ``GET /sessions/{id}/series``). An empty window returns ``count == 0`` and null
    statistics. The standard deviation is the population standard deviation; the
    percentiles use linear interpolation.
    """
    t0 = t0_of(all_series)
    lo = None if start_ms is None else t0 + start_ms
    hi = None if end_ms is None else t0 + end_ms
    selected = [
        name for name in (all_series if names is None else names) if not is_position_name(name)
    ]
    series: list[dict] = []
    for name in selected:
        data = all_series[name]
        times, values = window(data.times, data.values, lo, hi)
        if values:
            entry: dict[str, object] = {"name": name, "kind": data.kind, "unit": data.unit}
            entry.update(_stats(values))
            entry["t_first_ms"] = times[0] - t0
            entry["t_last_ms"] = times[-1] - t0
        else:
            entry = {
                "name": name,
                "kind": data.kind,
                "unit": data.unit,
                "count": 0,
                "mean": None,
                "std": None,
                "min": None,
                "max": None,
                "p05": None,
                "p50": None,
                "p95": None,
                "t_first_ms": None,
                "t_last_ms": None,
            }
        series.append(entry)
    return {
        "session_id": session_id,
        "t0_device_ms": t0,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "series": series,
    }


def _derived_series(session_dir: Path) -> list[SeriesData]:
    path = derived_path(session_dir)
    if not path.is_file():
        return []
    table = pq.read_table(path, columns=["device_time_ms", "signal", "value", "unit", "valid"])
    table = table.filter(
        pc.and_(
            pc.equal(table["valid"], True),
            pc.and_(pc.is_valid(table["value"]), pc.is_valid(table["device_time_ms"])),
        )
    )
    by_signal: dict[str, list[tuple[int, float, str | None]]] = {}
    for t, signal, v, unit in zip(
        table["device_time_ms"].to_pylist(),
        table["signal"].to_pylist(),
        table["value"].to_pylist(),
        table["unit"].to_pylist(),
        strict=True,
    ):
        if signal is not None and math.isfinite(v):
            by_signal.setdefault(signal, []).append((t, float(v), unit))
    out: list[SeriesData] = []
    for signal, rows in sorted(by_signal.items()):
        rows.sort(key=lambda r: r[0])
        unit = next((u for _, _, u in rows if u), None)
        out.append(
            SeriesData(
                f"derived.{signal}",
                "derived",
                unit,
                [r[0] for r in rows],
                [r[1] for r in rows],
            )
        )
    return out


def load_all(session_dir: Path) -> dict[str, SeriesData]:
    """Every available series of the session, keyed by name (did, sensor, derived order)."""
    inputs = SessionInputs(session_dir)
    found: list[SeriesData] = []
    for did_name, entry in vehicle_catalog.SIGNALS.items():
        s = inputs.series(f"did.{did_name}")
        if s is not None:
            found.append(SeriesData(f"did.{did_name}", "did", entry.get("unit"), s.times, s.values))
    for sensor_name, entry in vehicle_derived.SENSORS.items():
        s = inputs.series(f"sensor.{sensor_name}")
        if s is not None:
            found.append(
                SeriesData(f"sensor.{sensor_name}", "sensor", entry.get("unit"), s.times, s.values)
            )
    found.extend(_derived_series(session_dir))
    return {
        d.name: d
        for d in found
        if d.times and all(math.isfinite(v) for v in d.values) and not is_position_name(d.name)
    }


def t0_of(all_series: dict[str, SeriesData]) -> int:
    """Smallest first-sample time over all available series (0 if there are none)."""
    return min((d.times[0] for d in all_series.values()), default=0)


def build_response(
    session_id: str,
    all_series: dict[str, SeriesData],
    names: list[str] | None,
    max_points: int,
    from_ms: int | None,
    to_ms: int | None,
) -> dict:
    """The response body; names must already be validated against all_series."""
    t0 = t0_of(all_series)
    lo = None if from_ms is None else t0 + from_ms
    hi = None if to_ms is None else t0 + to_ms
    selected = list(all_series) if names is None else names
    series: list[dict] = []
    for name in selected:
        data = all_series[name]
        times, values = window(data.times, data.values, lo, hi)
        total = len(times)
        times, values = downsample_minmax(times, values, max_points)
        series.append(
            {
                "name": name,
                "kind": data.kind,
                "unit": data.unit,
                "points_total": total,
                "downsampled": len(times) < total,
                "t_ms": [t - t0 for t in times],
                "v": values,
            }
        )
    return {
        "session_id": session_id,
        "t0_device_ms": t0,
        "max_points": max_points,
        "series": series,
    }
