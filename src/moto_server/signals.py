"""Maps BLE telemetry signals to moto-vehicle-defs metadata (units, ranges).

Signal identity: a BLE schema field with a ``defsSignal`` key (e.g. "rpm" ->
"ENGINE_SPEED") names a moto_defs.vehicle_cl250.DIDS entry. We key everything
by the lowercased defs signal name (e.g. "engine_speed"), which is also used
verbatim as the parquet/report column prefix.
"""

from __future__ import annotations

from moto_server.defs import vehicle_cl250

# moto_defs.vehicle_cl250.DIDS[name] = (did, length, factor_num, factor_den,
# offset, unit, min, max, poll_ms) -- see gen/python/moto_defs/vehicle_cl250.py.
_DID_UNIT = 5
_DID_MIN = 6
_DID_MAX = 7

# Normalizes a defs unit string into a column-name-safe suffix.
_UNIT_SUFFIX = {
    "rpm": "rpm",
    "km/h": "kmh",
    "%": "pct",
    "degC": "degc",
    "V": "v",
}

# BLE schema `flags` bit name -> our signal key. Kept as an explicit table
# (rather than derived) because the bit names don't follow one mechanical
# rule (e.g. "speedValid" vs. the defs name "VEHICLE_SPEED").
FLAG_TO_SIGNAL = {
    "rpmValid": "engine_speed",
    "speedValid": "vehicle_speed",
    "coolantTempValid": "coolant_temp",
    "throttlePosValid": "throttle_pos",
    "batteryVoltValid": "battery_voltage",
}


def signal_key(defs_signal: str) -> str:
    """The lowercase key used for a defs signal name, e.g. "ENGINE_SPEED" -> "engine_speed"."""
    return defs_signal.lower()


_SIGNAL_TO_FLAG = {key: flag for flag, key in FLAG_TO_SIGNAL.items()}


def flag_name_for(defs_signal: str) -> str | None:
    """The `flags` bit name (e.g. "rpmValid") for a defs signal, or None if it has no bit."""
    return _SIGNAL_TO_FLAG.get(signal_key(defs_signal))


def unit_suffix(unit: str) -> str:
    if unit in _UNIT_SUFFIX:
        return _UNIT_SUFFIX[unit]
    # Fallback for a unit not yet in the table: lowercase, alnum-only.
    return "".join(ch for ch in unit.lower() if ch.isalnum()) or "unit"


def physical_column(defs_signal: str) -> str:
    unit = vehicle_cl250.DIDS[defs_signal][_DID_UNIT]
    return f"{signal_key(defs_signal)}_{unit_suffix(unit)}"


def age_column(defs_signal: str) -> str:
    return f"{signal_key(defs_signal)}_age_ms"


def valid_column(defs_signal: str) -> str:
    return f"{signal_key(defs_signal)}_valid"


def signal_range(defs_signal: str) -> tuple[float, float]:
    entry = vehicle_cl250.DIDS[defs_signal]
    return entry[_DID_MIN], entry[_DID_MAX]


def known_defs_signals(schema: dict) -> list[str]:
    """All defsSignal names referenced by the (v3) top-level telemetry fields,
    in field order. v3 is the superset layout, so this also covers v2 sessions
    (their physical/age/valid columns for signals absent from v2 are simply null).
    """
    seen: list[str] = []
    for field in schema["fields"]:
        name = field.get("defsSignal")
        if name and name not in seen:
            seen.append(name)
    return seen
