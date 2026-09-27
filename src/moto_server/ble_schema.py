"""Generic decoder for the BLE telemetry / IMU wire formats.

Driven entirely by the schema JSON (src/moto_server/schemas/ble_telemetry_packet_schema.json,
a verbatim copy of moto-connectivity-node's docs/ble_telemetry_packet_schema.json,
kept in sync by scripts/sync_ble_schema.sh) -- this module never hardcodes a
byte offset. See platform rule #2: signal layouts come only from the schema.
"""

from __future__ import annotations

import json
import struct
from functools import lru_cache
from importlib import resources
from typing import Any

_STRUCT_FORMAT = {
    "uint8": "B",
    "int8": "b",
    "uint16": "H",
    "int16": "h",
    "uint32": "I",
}


class DecodeError(ValueError):
    """A telemetry/IMU payload could not be decoded against the schema."""


@lru_cache(maxsize=1)
def load_default_schema() -> dict[str, Any]:
    """The schema bundled with this package (kept byte-identical to
    moto-connectivity-node's copy; see the schema drift test)."""
    raw = resources.files("moto_server.schemas").joinpath("ble_telemetry_packet_schema.json")
    return json.loads(raw.read_text())


def unpack_fields(raw: bytes, field_defs: list[dict]) -> dict[str, int]:
    """Decodes a flat, little-endian, packed struct from a schema field list."""
    values: dict[str, int] = {}
    for field in field_defs:
        fmt = _STRUCT_FORMAT.get(field["type"])
        if fmt is None:
            raise DecodeError(f"unsupported field type: {field['type']!r}")
        offset = field["offset"]
        size = field["size"]
        if offset + size > len(raw):
            raise DecodeError(
                f"field {field['name']!r} needs bytes [{offset}:{offset + size}), "
                f"payload is {len(raw)} bytes"
            )
        (value,) = struct.unpack_from("<" + fmt, raw, offset)
        values[field["name"]] = value
    return values


def pack_fields(values: dict[str, int], field_defs: list[dict], total_bytes: int) -> bytes:
    """The inverse of unpack_fields: builds a packed little-endian payload from
    a {field_name: int_value} map. Used by tests/make_fixtures.py to generate
    fixture raw_hex from the schema instead of hand-written byte offsets."""
    buf = bytearray(total_bytes)
    for field in field_defs:
        fmt = _STRUCT_FORMAT.get(field["type"])
        if fmt is None:
            raise DecodeError(f"unsupported field type: {field['type']!r}")
        struct.pack_into("<" + fmt, buf, field["offset"], values[field["name"]])
    return bytes(buf)


def decode_flags(value: int, bits: list[dict]) -> dict[str, bool]:
    return {
        bit["name"]: bool(value & (1 << bit["bit"])) for bit in bits if bit["name"] != "reserved"
    }


def decode_telemetry(raw: bytes, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Decodes one telemetry notification payload.

    Reads `version` first (per the schema's `versioning.rule`) and dispatches
    to the version 3 (`fields`) or version 2 (`lowMtuFallback.fields`) layout.
    Any other version raises DecodeError.
    """
    schema = schema or load_default_schema()
    if not raw:
        raise DecodeError("empty telemetry payload")

    version = raw[0]
    if version == schema["version"]:
        layout = schema
    elif version == schema["lowMtuFallback"]["version"]:
        layout = schema["lowMtuFallback"]
    else:
        raise DecodeError(f"unsupported telemetry packet version: {version}")

    field_defs = layout["fields"]
    total_bytes = layout["totalBytes"]
    if len(raw) != total_bytes:
        raise DecodeError(f"telemetry v{version}: expected {total_bytes} bytes, got {len(raw)}")

    raw_values = unpack_fields(raw, field_defs)

    physical: dict[str, float] = {}
    ages_ms: dict[str, int] = {}
    for field in field_defs:
        if field.get("deprecated"):
            continue
        if "defsSignal" in field:
            physical[field["defsSignal"]] = raw_values[field["name"]] * field.get("scale", 1)
        if "ageOf" in field:
            ages_ms[field["ageOf"]] = raw_values[field["name"]]

    flags = decode_flags(raw_values.get("flags", 0), schema["flags"]["bits"])

    can_health = None
    if "canBusState" in raw_values:
        bus_state_raw = raw_values["canBusState"]
        bus_state_name = next(
            (
                v["name"]
                for v in schema["canHealth"]["busState"]["values"]
                if v["value"] == bus_state_raw
            ),
            None,
        )
        can_flags_value = raw_values.get("canFlags", 0)
        can_health = {
            "bus_state": bus_state_name,
            "bus_state_raw": bus_state_raw,
            "tec": raw_values.get("canTxErrorCount"),
            "rec": raw_values.get("canRxErrorCount"),
            "bus_off_count": raw_values.get("canBusOffCount"),
            "unanswered_did_count": raw_values.get("unansweredDidCount"),
            "flags": decode_flags(can_flags_value, schema["canHealth"]["canFlags"]["bits"]),
            "flags_raw": can_flags_value,
        }

    return {
        "version": version,
        "seq": raw_values["seq"],
        "device_time_ms": raw_values.get("deviceTimeMs"),
        "raw": raw_values,
        "physical": physical,
        "ages_ms": ages_ms,
        "flags": flags,
        "can_health": can_health,
    }


def decode_imu_block(raw: bytes, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Decodes one IMU BLE notification payload (header + 1..maxSamples samples)."""
    schema = schema or load_default_schema()
    imu = schema["imuBlock"]
    header_defs = imu["headerFields"]
    header_bytes = imu["headerBytes"]
    sample_defs = imu["sampleFields"]
    sample_bytes = imu["sampleBytes"]

    if len(raw) < header_bytes:
        raise DecodeError(f"imu block: need at least {header_bytes} header bytes, got {len(raw)}")

    header_raw = unpack_fields(raw[:header_bytes], header_defs)
    version = header_raw["version"]
    if version != imu["version"]:
        raise DecodeError(f"unsupported IMU block version: {version}")

    sample_count = header_raw["sampleCount"]
    expected_len = header_bytes + sample_count * sample_bytes
    if len(raw) != expected_len:
        raise DecodeError(
            f"imu block: expected {expected_len} bytes for sampleCount={sample_count}, "
            f"got {len(raw)}"
        )

    flags = decode_flags(header_raw["flags"], imu["flags"]["bits"])
    accel_lsb = imu["scale"]["accel"]["lsbPerUnit"]
    gyro_lsb = imu["scale"]["gyro"]["lsbPerUnit"]
    sample_period_ms = header_raw["samplePeriodMs"]
    first_sample_index = header_raw["firstSampleIndex"]
    device_time_ms = header_raw["deviceTimeMs"]

    samples = []
    for i in range(sample_count):
        start = header_bytes + i * sample_bytes
        sample_raw = unpack_fields(raw[start : start + sample_bytes], sample_defs)
        samples.append(
            {
                "sample_index": (first_sample_index + i) % 65536,
                "device_time_ms": (device_time_ms + i * sample_period_ms) % (2**32),
                "ax_raw": sample_raw["ax"],
                "ay_raw": sample_raw["ay"],
                "az_raw": sample_raw["az"],
                "gx_raw": sample_raw["gx"],
                "gy_raw": sample_raw["gy"],
                "gz_raw": sample_raw["gz"],
                "ax_g": sample_raw["ax"] / accel_lsb,
                "ay_g": sample_raw["ay"] / accel_lsb,
                "az_g": sample_raw["az"] / accel_lsb,
                "gx_dps": sample_raw["gx"] / gyro_lsb,
                "gy_dps": sample_raw["gy"] / gyro_lsb,
                "gz_dps": sample_raw["gz"] / gyro_lsb,
            }
        )

    return {
        "version": version,
        "seq": header_raw["seq"],
        "device_time_ms": device_time_ms,
        "first_sample_index": first_sample_index,
        "sample_count": sample_count,
        "sample_period_ms": sample_period_ms,
        "flags": flags,
        "samples": samples,
    }
