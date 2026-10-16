"""Derived signals of a stored session: the Python reference of D-070 item 2.

derive_session() evaluates moto_defs.vehicle_derived over a session's parquet files and
returns the long-format table plus the coverage block; store() writes
<session_dir>/derived.parquet (session_id, device_time_ms, signal, value, unit, valid,
flag) and returns the `derived` block of report.json. Only class A signals with a formula in
formulas.py are computed; every other signal is listed in the coverage with its status.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from moto_server.defs import defs_version
from moto_server.derived import engine
from moto_server.derived.inputs import SessionInputs

DERIVED_FILE = "parquet/derived.parquet"

TABLE_SCHEMA = pa.schema(
    [
        ("session_id", pa.string()),
        ("device_time_ms", pa.int64()),
        ("signal", pa.string()),
        ("value", pa.float64()),
        ("unit", pa.string()),
        ("valid", pa.bool_()),
        ("flag", pa.string()),
    ]
)


@dataclass
class DeriveResult:
    table: pa.Table
    coverage: dict[str, Any]  # the `derived` block of report.json


def derived_path(session_dir: Path) -> Path:
    return session_dir / DERIVED_FILE


def _table(session_id: str, outputs: list[engine.SignalOutput]) -> pa.Table:
    times: list[int] = []
    signals: list[str] = []
    values: list[float | None] = []
    units: list[str] = []
    flags: list[str | None] = []
    for out in outputs:
        n = len(out.times)
        times += out.times
        signals += [out.name] * n
        values += out.values
        units += [out.unit] * n
        flags += out.flags
    return pa.table(
        {
            "session_id": pa.array([session_id] * len(times), type=pa.string()),
            "device_time_ms": pa.array(times, type=pa.int64()),
            "signal": pa.array(signals, type=pa.string()),
            "value": pa.array(values, type=pa.float64()),
            "unit": pa.array(units, type=pa.string()),
            "valid": pa.array([v is not None for v in values], type=pa.bool_()),
            "flag": pa.array(flags, type=pa.string()),
        },
        schema=TABLE_SCHEMA,
    )


def derive_session(session_dir: Path, session_id: str) -> DeriveResult:
    """Evaluates all derived signals of the session stored under `session_dir`."""
    evaluation = engine.evaluate(SessionInputs(session_dir))
    statuses: dict[str, int] = {}
    for entry in evaluation.coverage.values():
        statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
    coverage = {
        "defs_version": defs_version(),
        "summary": statuses,
        "rows": sum(len(o.times) for o in evaluation.outputs),
        "valid_rows": sum(o.valid_count for o in evaluation.outputs),
        "signals": evaluation.coverage,
    }
    return DeriveResult(_table(session_id, evaluation.outputs), coverage)


def store(session_dir: Path, session_id: str) -> tuple[dict[str, Any], bool]:
    """Derives and writes derived.parquet (removed when there is no row). Returns the
    `derived` report block and whether the file exists. Any exception becomes
    {"error": ...} (and no file): derived signals never fail an ingest."""
    path = derived_path(session_dir)
    try:
        result = derive_session(session_dir, session_id)
        if result.table.num_rows:
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(result.table, path)
        else:
            path.unlink(missing_ok=True)
        return result.coverage, result.table.num_rows > 0
    except Exception as exc:
        path.unlink(missing_ok=True)
        return {"error": f"{type(exc).__name__}: {exc}"}, False
