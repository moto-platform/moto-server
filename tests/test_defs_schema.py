"""The BLE schema comes from moto-vehicle-defs (D-061); this repo keeps no copy.

Replaces the old byte-equality drift test against moto-connectivity-node's copy.
"""

from __future__ import annotations

from moto_server import ble_schema, signals
from moto_server.defs import ble as defs_ble


def test_loaded_schema_is_the_defs_schema():
    schema = ble_schema.load_default_schema()
    assert schema == defs_ble.SCHEMA
    # A deep copy: mutating what the decoder uses never touches the generated module.
    assert schema is not defs_ble.SCHEMA
    assert schema["fields"] is not defs_ble.SCHEMA["fields"]


def test_schema_version_and_totals():
    schema = ble_schema.load_default_schema()
    assert schema["packetName"] == "BLETelemetryPacket"
    assert schema["version"] == defs_ble.CURRENT_VERSION == 4
    assert schema["totalBytes"] == 57
    assert defs_ble.TOTAL_BYTES_BY_VERSION == {3: 37, 4: 57}
    assert {int(k): v for k, v in schema["totalBytesByVersion"].items()} == {3: 37, 4: 57}
    assert schema["lowMtuFallback"]["version"] == defs_ble.FALLBACK_VERSION == 2
    assert tuple(ble_schema.accepted_versions(schema)) == defs_ble.ACCEPTED_VERSIONS


def test_known_defs_signals_from_the_schema():
    assert signals.known_defs_signals(ble_schema.load_default_schema()) == [
        "ENGINE_SPEED",
        "VEHICLE_SPEED",
        "COOLANT_TEMP",
        "THROTTLE_POS",
        "BATTERY_VOLTAGE",
    ]
