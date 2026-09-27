"""Schema drift test: src/moto_server/schemas/ble_telemetry_packet_schema.json
must stay byte-identical to moto-connectivity-node's copy (the single source
of truth). See scripts/sync_ble_schema.sh and CI's optional MOTO_CONN_READ_TOKEN
fetch step (same pattern as moto-mobile's CI).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from moto_server import ble_schema, signals

PACKAGED_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "moto_server"
    / "schemas"
    / "ble_telemetry_packet_schema.json"
)


def test_packaged_schema_parses_and_decoder_builds_from_it():
    """(b) always checks the copy parses and the decoder builds from it."""
    schema = ble_schema.load_default_schema()
    assert schema["packetName"] == "BLETelemetryPacket"
    assert schema["version"] == 3
    assert schema["totalBytes"] == 37
    assert schema["lowMtuFallback"]["version"] == 2
    assert signals.known_defs_signals(schema) == [
        "ENGINE_SPEED",
        "VEHICLE_SPEED",
        "COOLANT_TEMP",
        "THROTTLE_POS",
        "BATTERY_VOLTAGE",
    ]


def test_schema_matches_moto_connectivity_node_when_available():
    """(a) when MOTO_CONN_SCHEMA is set, assert byte equality; fail (rather
    than skip) if MOTO_SCHEMA_DRIFT_REQUIRED=1 and it is missing."""
    conn_schema_path = os.environ.get("MOTO_CONN_SCHEMA")
    drift_required = os.environ.get("MOTO_SCHEMA_DRIFT_REQUIRED") == "1"

    if not conn_schema_path:
        if drift_required:
            pytest.fail(
                "MOTO_SCHEMA_DRIFT_REQUIRED=1 but MOTO_CONN_SCHEMA is not set: "
                "cannot verify the schema copy against moto-connectivity-node"
            )
        pytest.skip("MOTO_CONN_SCHEMA not set: schema drift check skipped locally")

    conn_bytes = Path(conn_schema_path).read_bytes()
    packaged_bytes = PACKAGED_SCHEMA_PATH.read_bytes()
    assert conn_bytes == packaged_bytes, (
        "src/moto_server/schemas/ble_telemetry_packet_schema.json has drifted from "
        "moto-connectivity-node's docs/ble_telemetry_packet_schema.json; "
        "run scripts/sync_ble_schema.sh and commit the result"
    )
