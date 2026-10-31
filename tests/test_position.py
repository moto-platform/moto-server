"""The shared position-name rule (D-076, invariant 7)."""

from __future__ import annotations

import pytest

from vehicle_server.defs import vehicle_catalog, vehicle_derived
from vehicle_server.position import is_position_name


@pytest.mark.parametrize(
    "name",
    [
        "derived.gps_latitude",
        "sensor.gps_lon",
        "sensor.gps_altitude_m",
        "derived.height_above_ground",
        "sensor.hMSL",
        "sensor.gpsLat",
        "derived.ecef_x",
        "derived.lat_change",
    ],
)
def test_position_names_are_refused(name: str):
    assert is_position_name(name)


@pytest.mark.parametrize(
    "name", ["did.THROTTLE_POS", "sensor.throttle_pos_pct", "derived.altitude_change"]
)
def test_non_position_names_pass(name: str):
    assert not is_position_name(name)


def test_no_defs_series_name_looks_like_a_position():
    names = (
        [f"did.{name}" for name in vehicle_catalog.SIGNALS]
        + [f"sensor.{name}" for name in vehicle_derived.SENSORS]
        + [f"derived.{name}" for name in vehicle_derived.SIGNALS]
    )
    assert [name for name in names if is_position_name(name)] == []
