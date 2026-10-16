"""Formulas of the first batch of derived signals, against hand-computed values."""

from __future__ import annotations

import math

import pytest

from moto_server.defs import vehicle_catalog, vehicle_constants, vehicle_derived
from moto_server.derived import engine
from moto_server.derived.context import InputInvalid
from moto_server.derived.formulas import FORMULAS
from moto_server.derived.inputs import STANDARD_GRAVITY_MPS2, resolve_sensor

from .derived_helpers import FakeInputs, const_series, env_for, point_value, ramp

SPEED = "did.VEHICLE_SPEED"
RPM = "did.ENGINE_SPEED"
KMH_36 = 36.0  # 10 m/s


def _outputs(inputs: dict, only: str):
    evaluation = engine.evaluate(FakeInputs(inputs))
    return {o.name: o for o in evaluation.outputs}[only], evaluation


# --- the registry matches the defs ---------------------------------------------------------


def test_every_formula_is_a_class_a_signal_with_resolvable_inputs():
    names = set()
    for name in FORMULAS:
        spec = vehicle_derived.SIGNALS[name]
        assert spec["class"] == "A" and not spec.get("event")
        assert spec["ready_phase0"] and not spec.get("pending_inputs")
        for inp in spec["inputs"]:
            kind, _, key = inp.partition(".")
            names.add(inp)
            if kind == "did":
                assert key in vehicle_catalog.SIGNALS, inp
            elif kind == "const":
                assert key in vehicle_constants.CONSTANTS, inp
            elif kind == "derived":
                assert key in vehicle_derived.SIGNALS, inp
            else:
                assert kind == "sensor" and key in vehicle_derived.SENSORS, inp
                assert resolve_sensor(key) is not None, f"{inp} has no parquet column"
    assert len(FORMULAS) == 19
    assert "did.VEHICLE_SPEED" in names


def test_formulas_only_read_declared_inputs():
    """Env.feed raises KeyError for an undeclared input: run every formula once over a
    feed for each declared input and require that nothing but a data problem is raised."""
    for name, formula in FORMULAS.items():
        spec = vehicle_derived.SIGNALS[name]
        feeds = {
            inp: const_series(0, 5000, 10, 1.0)
            for inp in spec["inputs"]
            if not inp.startswith("const.")
        }
        try:
            if formula.kind == "point":
                point_value(name, feeds, 5000)
            else:
                formula.fn(env_for(name, feeds), [5000])
        except (InputInvalid, ArithmeticError):
            pass


# --- speed derivatives ---------------------------------------------------------------------


def test_vehicle_accel_is_the_speed_slope_over_the_window():
    # +1 m/s every 110 ms = 3.6 km/h steps -> 1 / 0.11 s = 9.0909 m/s2
    speed = ramp(0, 880, 110, lambda t: 3.6 * t / 110)
    assert point_value("vehicle_accel", {SPEED: speed}, 880) == pytest.approx(10 / 1.1)


def test_vehicle_accel_needs_two_samples_in_the_window():
    with pytest.raises(InputInvalid) as exc:
        point_value("vehicle_accel", {SPEED: ([0, 100], [10.0, 12.0])}, 500)
    assert exc.value.flag == "stale_input"  # newest sample 400 ms old (limit 330)
    with pytest.raises(InputInvalid) as exc:
        point_value("vehicle_accel", {SPEED: ([100], [10.0])}, 100)
    assert exc.value.flag == "insufficient_samples"


def test_vehicle_jerk_is_the_slope_of_the_acceleration():
    accel = ramp(0, 880, 110, lambda t: t / 110)  # 1 m/s2 per 110 ms
    assert point_value("vehicle_jerk", {"derived.vehicle_accel": accel}, 880) == pytest.approx(
        1 / 0.11
    )


def test_accel_imu_residual_is_window_mean_minus_speed_acceleration():
    imu = ramp(0, 1000, 10, lambda _t: 2.0)
    value = point_value(
        "accel_imu_residual",
        {"sensor.imu_accel_x": imu, "derived.vehicle_accel": ([900], [1.5])},
        1000,
    )
    assert value == pytest.approx(0.5)


# --- gearbox ---------------------------------------------------------------------------------


def test_gearbox_ratio_from_rpm_speed_and_the_defs_constants():
    # 36 km/h = 10 m/s, r = 0.311 m, primary 2.807, secondary 2.642, 3000 rpm
    value = point_value("gearbox_ratio_measured", {RPM: ([0], [3000.0]), SPEED: ([0], [KMH_36])}, 0)
    assert value == pytest.approx(1.31745, abs=1e-4)
    r = vehicle_constants.CONSTANTS["rear_wheel_radius_dynamic_m"][0]
    wheel_rpm = 10 / (2 * math.pi * r) * 60
    reduction = (
        vehicle_constants.CONSTANTS["primary_reduction_ratio"][0]
        * vehicle_constants.CONSTANTS["secondary_reduction_ratio"][0]
    )
    assert value == pytest.approx(3000 / (wheel_rpm * reduction))


def test_gearbox_ratio_is_invalid_when_standing():
    with pytest.raises(InputInvalid) as exc:
        point_value("gearbox_ratio_measured", {RPM: ([0], [1500.0]), SPEED: ([0], [0.0])}, 0)
    assert exc.value.flag == "standstill"


@pytest.mark.parametrize(
    ("ratio", "gear"),
    [
        (3.416, 1.0),
        (1.3174, 4.0),  # 2.4 % below the 4th ratio 1.35
        (1.20, 5.0),  # 2.9 % above the 5th ratio 1.166
        (1.25, 0.0),  # 7.2 % from 5th and 7.4 % from 4th: no gear
        (6.0, 0.0),
    ],
)
def test_gear_estimate_is_the_nearest_gear_within_the_tolerance(ratio: float, gear: float):
    feed = {"derived.gearbox_ratio_measured": ([0], [ratio])}
    assert point_value("gear_estimate", feed, 0) == gear


# --- distance and time -------------------------------------------------------------------------


def test_trip_distance_integrates_the_speed():
    # 10 m/s for 3 s, a sample every 100 ms: 20 m at 2000 ms, 30 m at 3000 ms
    out, _ = _outputs({SPEED: const_series(0, 3000, 100, KMH_36)}, "trip_distance")
    assert out.times == [0, 1000, 2000, 3000]
    assert out.values == pytest.approx([0.0, 0.01, 0.02, 0.03])


def test_trip_distance_does_not_integrate_a_gap_longer_than_the_stale_limit():
    # speed samples 0..1000 and 9000..10000: the pre-gap sample is held for stale_after_ms
    # (3000 ms) only, so 1 s + 3 s + 1 s at 10 m/s = 50 m; 4000..9000 adds nothing.
    times = [*range(0, 1001, 100), *range(9000, 10001, 100)]
    out, _ = _outputs({SPEED: (times, [KMH_36] * len(times))}, "trip_distance")
    by_time = dict(zip(out.times, out.values, strict=True))
    assert by_time[10000] == pytest.approx(0.05)
    assert by_time[5000] is None  # stale while the gap lasts, never a held value
    assert out.flags[out.times.index(5000)] == "stale_input"


def test_trip_time_counts_the_engine_running():
    times = list(range(0, 2001, 100))
    rpm = [1000.0 if t < 1000 else 0.0 for t in times]
    out, _ = _outputs({RPM: (times, rpm)}, "trip_time")
    assert dict(zip(out.times, out.values, strict=True))[2000] == pytest.approx(1.0)


def test_idle_time_counts_standing_with_the_engine_running():
    times = list(range(0, 2001, 100))
    speed = [0.0 if t < 1000 else KMH_36 for t in times]
    out, _ = _outputs({SPEED: (times, speed), RPM: (times, [1000.0] * len(times))}, "idle_time")
    assert dict(zip(out.times, out.values, strict=True))[2000] == pytest.approx(1.0)


# --- yaw rate, lateral acceleration, turn radius ------------------------------------------------


def test_yaw_rate_is_the_gz_mean_over_the_window():
    gz = ramp(0, 200, 10, lambda t: t / 10)  # window (0, 100]: gz = 1..10
    assert point_value("yaw_rate", {"sensor.imu_gyro_z": gz}, 100) == pytest.approx(5.5)


def test_lateral_acceleration_is_speed_times_yaw_rate():
    feeds = {"derived.yaw_rate": ([0], [18.0]), SPEED: ([0], [KMH_36])}  # 10 m/s, 18 deg/s
    assert point_value("lateral_accel", feeds, 0) == pytest.approx(math.pi)  # 10 * 0.31416
    feeds = {"derived.yaw_rate": ([0], [-18.0]), SPEED: ([0], [KMH_36])}
    assert point_value("lateral_accel", feeds, 0) == pytest.approx(-math.pi)


def test_turn_radius_is_speed_over_yaw_rate_and_invalid_when_straight():
    feeds = {"derived.yaw_rate": ([0], [-18.0]), SPEED: ([0], [KMH_36])}
    assert point_value("turn_radius", feeds, 0) == pytest.approx(31.831, abs=1e-3)
    feeds = {"derived.yaw_rate": ([0], [0.2]), SPEED: ([0], [KMH_36])}
    with pytest.raises(InputInvalid) as exc:
        point_value("turn_radius", feeds, 0)
    assert exc.value.flag == "straight_line"


def test_turn_radius_beyond_the_range_is_null_out_of_range():
    # 0.6 deg/s at 10 m/s -> 955 m is fine; standing still gives 0 m, below the 3 m minimum
    inputs = {
        "sensor.imu_gyro_z": const_series(0, 400, 10, 0.6),
        SPEED: const_series(0, 400, 10, 0.0),
    }
    out, _ = _outputs(inputs, "turn_radius")
    assert set(out.flags) == {"out_of_range"} and set(out.values) == {None}


# --- torque and power ---------------------------------------------------------------------------


def test_torques_are_percent_times_reference():
    ref = ([0], [23.0])
    actual = point_value(
        "engine_torque", {"did.ACTUAL_TORQUE_PCT": ([0], [50.0]), "did.REFERENCE_TORQUE": ref}, 0
    )
    assert actual == pytest.approx(11.5)
    demand = point_value(
        "demand_torque", {"did.DEMAND_TORQUE_PCT": ([0], [60.0]), "did.REFERENCE_TORQUE": ref}, 0
    )
    assert demand == pytest.approx(13.8)
    negative = point_value(
        "engine_torque", {"did.ACTUAL_TORQUE_PCT": ([0], [-10.0]), "did.REFERENCE_TORQUE": ref}, 0
    )
    assert negative == pytest.approx(-2.3)


def test_torque_gap_is_demand_minus_actual():
    feeds = {"derived.demand_torque": ([0], [13.8]), "derived.engine_torque": ([0], [11.5])}
    assert point_value("torque_gap", feeds, 0) == pytest.approx(2.3)


def test_engine_power_is_torque_times_angular_speed():
    feeds = {"derived.engine_torque": ([0], [10.0]), RPM: ([0], [6000.0])}
    assert point_value("engine_power", feeds, 0) == pytest.approx(6.2832, abs=1e-4)  # 10 * 628.3 W


# --- air and fuel -------------------------------------------------------------------------------


def test_air_density_is_the_ideal_gas_density():
    feeds = {"did.BAROMETRIC_PRESSURE": ([0], [101.0]), "did.INTAKE_AIR_TEMP": ([0], [20.0])}
    assert point_value("air_density", feeds, 0) == pytest.approx(1.20022, abs=1e-5)


def test_power_correction_factor_is_the_density_ratio_to_the_j1349_reference():
    # rho_ref = 99 kPa, 25 degC = 1.15673 kg/m3; at 1.20022 kg/m3 -> 0.96376
    value = point_value("power_correction_factor", {"derived.air_density": ([0], [1.20022])}, 0)
    assert value == pytest.approx(0.96376, abs=1e-4)
    standard = point_value("power_correction_factor", {"derived.air_density": ([0], [1.15673])}, 0)
    assert standard == pytest.approx(1.0, abs=1e-4)


def test_air_fuel_ratio_is_lambda_times_the_stoichiometric_ratio():
    assert point_value("air_fuel_ratio", {"did.COMMANDED_EQUIV_RATIO": ([0], [1.0])}, 0) == (
        pytest.approx(14.7)
    )
    assert point_value("air_fuel_ratio", {"did.COMMANDED_EQUIV_RATIO": ([0], [0.9])}, 0) == (
        pytest.approx(13.23)
    )


def test_fuel_rate_from_air_mass_flow_and_afr():
    # 2 g/s = 7.2 kg/h of air; AFR 14.7; fuel density 0.745 kg/L -> 0.65744 L/h
    feeds = {
        "did.MAF_RATE": ([0], [2.0]),
        "did.COMMANDED_EQUIV_RATIO": ([0], [1.0]),
    }
    assert point_value("fuel_rate_maf", feeds, 0) == pytest.approx(0.65744, abs=1e-5)
    with pytest.raises(InputInvalid):
        point_value("fuel_rate_maf", {**feeds, "did.COMMANDED_EQUIV_RATIO": ([0], [0.0])}, 0)


# --- sensors ------------------------------------------------------------------------------------


def test_imu_acceleration_is_converted_from_g_to_mps2():
    source = resolve_sensor("imu_accel_x")
    assert (source.file, source.column) == ("imu", "ax_g")
    assert source.factor == STANDARD_GRAVITY_MPS2
    assert resolve_sensor("imu_gyro_z").column == "gz_dps"
    assert resolve_sensor("imu_gyro_z").factor == 1.0
