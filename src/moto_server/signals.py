"""Maps BLE telemetry signals to moto-vehicle-defs metadata (units, ranges).

Signal identity: a BLE schema field with a ``defsSignal`` key (e.g. "rpm" ->
"ENGINE_SPEED") names a moto_defs.vehicle_cl250.DIDS entry. We key everything
by the lowercased defs signal name (e.g. "engine_speed"), which is also used
verbatim as the parquet/report column prefix.
"""

from __future__ import annotations

from moto_server.defs import vehicle_catalog, vehicle_cl250

# moto_defs.vehicle_cl250.DIDS[name] = (did, length, factor_num, factor_den,
# offset, unit, min, max, poll_ms, ...) -- see gen/python/moto_defs/vehicle_cl250.py.
# Only the indexes below are read here; trailing entries are ignored.
_DID_ID = 0
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


def did_name(did: int) -> str | None:
    """The defs signal name of a UDS DID (generated DIDS table), or None if unknown."""
    for name, entry in vehicle_cl250.DIDS.items():
        if entry[_DID_ID] == did:
            return name
    return None


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


def catalog_signal(did: int) -> tuple[str, dict] | None:
    """(name, catalog entry) of a DID in the generated vehicle catalog (D-066: the polled
    DIDs and the candidates alike), or None for a DID this defs version does not know."""
    name = vehicle_catalog.BY_DID.get(did)
    return (name, vehicle_catalog.SIGNALS[name]) if name else None


def catalog_raw_fits(entry: dict, raw: int) -> bool:
    """Whether a raw value fits the DID's data length (vehicleSignalBlock.decodeRule)."""
    return raw < 1 << (8 * entry["length"])


def catalog_value(name: str, raw: int) -> float:
    """Physical value of a raw record value, through the catalog's own decode()
    (raw * factor_num / factor_den + offset); the caller checks catalog_raw_fits() first."""
    entry = vehicle_catalog.SIGNALS[name]
    return vehicle_catalog.decode(name, raw.to_bytes(entry["length"], "big"))
