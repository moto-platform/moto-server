"""The formula of each implemented derived signal (D-070 item 2, class A, first batch).

Names, units, ranges, periods, windows and stale limits come from
moto_defs.vehicle_derived.SIGNALS; vehicle constants from moto_defs.vehicle_constants.
This module only holds the arithmetic, which follows moto-vehicle-defs
docs/vehicle-data-catalog.md section 5 and each signal's description in the defs. The input
names used below are checked against the defs by tests (every one must be a declared input
of its signal and must resolve).

Two kinds of formula:
  point   fn(at) -> float, evaluated on the signal's output grid; raises InputInvalid
          (flag) when an input is unusable.
  series  fn(env, grid) -> [(value | None, flag | None)] per grid time, for the
          integrals (distance, trip time, idle time) that carry state along the time axis.
Unit conversions and physics constants are named below; they are not vehicle data.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass

from moto_server.derived.context import (
    FLAG_INSUFFICIENT_SAMPLES,
    FLAG_INVALID_INPUT,
    At,
    Env,
    InputInvalid,
)

# --- unit conversions and physics (not vehicle constants) -----------------------------
KMH_TO_MPS = 1.0 / 3.6
RPM_TO_RAD_PER_S = 2.0 * math.pi / 60.0
DEG_TO_RAD = math.pi / 180.0
# Specific gas constant of dry air, J/(kg*K) (ideal-gas law rho = p / (R * T)).
SPECIFIC_GAS_CONSTANT_DRY_AIR = 287.058
KELVIN_OFFSET = 273.15
# SAE J1349 reference conditions of the power correction: 99 kPa dry-air pressure, 25 degC.
J1349_REFERENCE_PRESSURE_KPA = 99.0
J1349_REFERENCE_TEMPERATURE_C = 25.0

# --- tuning of the estimators (assumptions of this reference, documented in the README) -
# A measured gearbox ratio matches a gear when it is within this relative distance of the
# gear's ratio; the closest gear ratios (5th/6th) are 12 % apart, so 5 % cannot overlap.
GEAR_MATCH_TOLERANCE = 0.05
# Below this yaw rate the turn radius is not defined (going straight; gyro noise floor).
TURN_RADIUS_MIN_YAW_RATE_DPS = 0.5

FLAG_STANDSTILL = "standstill"
FLAG_STRAIGHT_LINE = "straight_line"

# --- input names (all of them declared by the defs; tests check this) -------------------
_RPM = "did.ENGINE_SPEED"
_SPEED = "did.VEHICLE_SPEED"
_TORQUE_ACTUAL_PCT = "did.ACTUAL_TORQUE_PCT"
_TORQUE_DEMAND_PCT = "did.DEMAND_TORQUE_PCT"
_TORQUE_REFERENCE = "did.REFERENCE_TORQUE"
_BARO = "did.BAROMETRIC_PRESSURE"
_IAT = "did.INTAKE_AIR_TEMP"
_LAMBDA = "did.COMMANDED_EQUIV_RATIO"
_MAF = "did.MAF_RATE"
_IMU_AX = "sensor.imu_accel_x"
_IMU_GZ = "sensor.imu_gyro_z"
_RATIO = "derived.gearbox_ratio_measured"
_ACCEL = "derived.vehicle_accel"
_YAW = "derived.yaw_rate"
_TORQUE = "derived.engine_torque"
_TORQUE_DEMAND = "derived.demand_torque"
_DENSITY = "derived.air_density"
_GEAR_PREFIX = "const.gear_ratio_"

PointFn = Callable[[At], float]
Outcome = tuple[float | None, str | None]
SeriesFn = Callable[[Env, list[int]], list[Outcome]]


@dataclass(frozen=True)
class Formula:
    kind: str  # "point" | "series"
    fn: Callable


FORMULAS: dict[str, Formula] = {}


def _point(name: str) -> Callable[[PointFn], PointFn]:
    def register(fn: PointFn) -> PointFn:
        FORMULAS[name] = Formula("point", fn)
        return fn

    return register


def _series(name: str) -> Callable[[SeriesFn], SeriesFn]:
    def register(fn: SeriesFn) -> SeriesFn:
        FORMULAS[name] = Formula("series", fn)
        return fn

    return register


# --- helpers -----------------------------------------------------------------------------


def air_density_kg_m3(pressure_kpa: float, temperature_c: float) -> float:
    """Ideal-gas density of dry air, rho = p / (R * T)."""
    return pressure_kpa * 1000.0 / (SPECIFIC_GAS_CONSTANT_DRY_AIR * (temperature_c + KELVIN_OFFSET))


def slope_per_s(points: list[tuple[int, float]]) -> float:
    """Least-squares slope (unit per second) of (time_ms, value) samples."""
    if len(points) < 2:
        raise InputInvalid(FLAG_INSUFFICIENT_SAMPLES)
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var == 0:
        raise InputInvalid(FLAG_INSUFFICIENT_SAMPLES)
    return sum((t - mean_t) * (v - mean_v) for t, v in points) / var * 1000.0


def mean(points: list[tuple[int, float]]) -> float:
    if not points:
        raise InputInvalid(FLAG_INSUFFICIENT_SAMPLES)
    return sum(v for _, v in points) / len(points)


# --- class A: speed derivatives ------------------------------------------------------------


@_point("vehicle_accel")
def vehicle_accel(at: At) -> float:
    """d(speed)/dt: least-squares slope of the ECU speed over window_ms (filtered)."""
    return slope_per_s([(t, v * KMH_TO_MPS) for t, v in at.window(_SPEED)])


@_point("vehicle_jerk")
def vehicle_jerk(at: At) -> float:
    """Slope of the filtered acceleration over window_ms."""
    return slope_per_s(at.window(_ACCEL))


@_point("accel_imu_residual")
def accel_imu_residual(at: At) -> float:
    """IMU longitudinal acceleration (mean over the same window) minus the speed-derived
    acceleration. Gravity along the pitch axis is not removed (cross-check only)."""
    return mean(at.window(_IMU_AX)) - at.value(_ACCEL)


# --- class A: gearbox ------------------------------------------------------------------------


@_point("gearbox_ratio_measured")
def gearbox_ratio_measured(at: At) -> float:
    """Engine revolutions per gearbox output revolution:
    rpm / (wheel rpm * primary * secondary), wheel rpm from the speed and the dynamic radius."""
    speed = at.value(_SPEED) * KMH_TO_MPS
    if speed <= 0:
        raise InputInvalid(FLAG_STANDSTILL)
    radius = at.const("const.rear_wheel_radius_dynamic_m")
    wheel_rpm = speed / (2.0 * math.pi * radius) * 60.0
    reduction = at.const("const.primary_reduction_ratio") * at.const(
        "const.secondary_reduction_ratio"
    )
    return at.value(_RPM) / (wheel_rpm * reduction)


@_point("gear_estimate")
def gear_estimate(at: At) -> float:
    """The gear (1..n) whose ratio is nearest to the measured one, 0 when none is within
    GEAR_MATCH_TOLERANCE (relative)."""
    measured = at.value(_RATIO)
    gears = {
        int(name.removeprefix(_GEAR_PREFIX)): at.const(name)
        for name in at.env.inputs
        if name.startswith(_GEAR_PREFIX)
    }
    gear, ratio = min(gears.items(), key=lambda item: abs(measured / item[1] - 1.0))
    return float(gear) if abs(measured / ratio - 1.0) <= GEAR_MATCH_TOLERANCE else 0.0


# --- class A: distance and time (integrals along the time axis) -----------------------------


def _integrate(
    env: Env, names: list[str], rate: Callable[[list[float]], float], grid: list[int]
) -> list[Outcome]:
    """Cumulative integral of rate(inputs) per second over the whole session.

    The rate is held between breakpoints (the union of the input sample times), but never
    longer than the shortest remaining freshness of the inputs, so a gap in the data adds
    nothing. The output at a grid time is null while any input is stale there. The
    integral starts when every input first exists (session start = key-on)."""
    feeds = [env.feed(n) for n in names]
    breakpoints = sorted({t for f in feeds for t in f.times})
    starts: list[int] = []
    rates: list[float] = []
    spans: list[float] = []
    for j, b in enumerate(breakpoints):
        at = At(env, b)
        try:
            values = [at.value(n) for n in names]
        except InputInvalid:
            continue
        remaining = min(f.times[f.latest(b)] + f.stale_after_ms - b for f in feeds)
        until_next = breakpoints[j + 1] - b if j + 1 < len(breakpoints) else 0
        starts.append(b)
        rates.append(rate(values))
        spans.append(max(0, min(until_next, remaining)) / 1000.0)
    prefix = [0.0]
    for r, s in zip(rates, spans, strict=True):
        prefix.append(prefix[-1] + r * s)

    out: list[Outcome] = []
    for t in grid:
        try:
            at = At(env, t)
            for n in names:
                at.value(n)
        except InputInvalid as exc:
            out.append((None, exc.flag))
            continue
        j = bisect_right(starts, t) - 1
        if j < 0:
            out.append((0.0, None))
            continue
        out.append((prefix[j] + rates[j] * min((t - starts[j]) / 1000.0, spans[j]), None))
    return out


@_series("trip_distance")
def trip_distance(env: Env, grid: list[int]) -> list[Outcome]:
    """Integral of the ECU speed, km (km/h divided by 3600 s/h per second)."""
    return _integrate(env, [_SPEED], lambda v: v[0] / 3600.0, grid)


@_series("trip_time")
def trip_time(env: Env, grid: list[int]) -> list[Outcome]:
    """Seconds with the engine running (RPM above 0)."""
    return _integrate(env, [_RPM], lambda v: 1.0 if v[0] > 0 else 0.0, grid)


@_series("idle_time")
def idle_time(env: Env, grid: list[int]) -> list[Outcome]:
    """Seconds with the engine running and the vehicle standing (speed 0, RPM above 0)."""
    return _integrate(env, [_SPEED, _RPM], lambda v: 1.0 if v[0] == 0 and v[1] > 0 else 0.0, grid)


# --- class A: yaw rate, lateral acceleration, turn radius ------------------------------------


@_point("yaw_rate")
def yaw_rate(at: At) -> float:
    """Mean of the IMU z gyro (deg/s) over window_ms. The IMU z axis is taken as the
    vehicle yaw axis (no mounting or lean correction)."""
    return mean(at.window(_IMU_GZ))


@_point("lateral_accel")
def lateral_accel(at: At) -> float:
    """a_lat = v * omega (v in m/s, omega in rad/s)."""
    return at.value(_SPEED) * KMH_TO_MPS * at.value(_YAW) * DEG_TO_RAD


@_point("turn_radius")
def turn_radius(at: At) -> float:
    """r = v / |omega|, m; the direction of the turn is the sign of the yaw rate."""
    yaw = at.value(_YAW)
    if abs(yaw) < TURN_RADIUS_MIN_YAW_RATE_DPS:
        raise InputInvalid(FLAG_STRAIGHT_LINE)
    return at.value(_SPEED) * KMH_TO_MPS / (abs(yaw) * DEG_TO_RAD)


# --- class A: torque and power ----------------------------------------------------------------


@_point("engine_torque")
def engine_torque(at: At) -> float:
    """Nm = actual torque % * reference torque / 100."""
    return at.value(_TORQUE_ACTUAL_PCT) * at.value(_TORQUE_REFERENCE) / 100.0


@_point("demand_torque")
def demand_torque(at: At) -> float:
    """Nm = demand torque % * reference torque / 100."""
    return at.value(_TORQUE_DEMAND_PCT) * at.value(_TORQUE_REFERENCE) / 100.0


@_point("torque_gap")
def torque_gap(at: At) -> float:
    """Demanded minus actual torque, Nm."""
    return at.value(_TORQUE_DEMAND) - at.value(_TORQUE)


@_point("engine_power")
def engine_power(at: At) -> float:
    """P = T * omega, kW."""
    return at.value(_TORQUE) * at.value(_RPM) * RPM_TO_RAD_PER_S / 1000.0


# --- class A: air and fuel ----------------------------------------------------------------------


@_point("air_density")
def air_density(at: At) -> float:
    """rho = p / (R * T) from the barometric pressure and the intake air temperature
    (dry air; the intake temperature stands in for the ambient one, as the defs list)."""
    return air_density_kg_m3(at.value(_BARO), at.value(_IAT))


@_point("power_correction_factor")
def power_correction_factor(at: At) -> float:
    """Density-ratio form of the SAE J1349 correction: rho_ref / rho, with rho_ref the dry-air
    density at the J1349 reference conditions (99 kPa, 25 degC)."""
    reference = air_density_kg_m3(J1349_REFERENCE_PRESSURE_KPA, J1349_REFERENCE_TEMPERATURE_C)
    return reference / at.value(_DENSITY)


@_point("air_fuel_ratio")
def air_fuel_ratio(at: At) -> float:
    """AFR = lambda * stoichiometric AFR."""
    return at.value(_LAMBDA) * at.const("const.stoichiometric_afr")


@_point("fuel_rate_maf")
def fuel_rate_maf(at: At) -> float:
    """L/h = MAF / (AFR * fuel density); MAF in g/s is 3.6 kg/h per g/s."""
    afr = at.value(_LAMBDA) * at.const("const.stoichiometric_afr")
    if afr <= 0:
        raise InputInvalid(FLAG_INVALID_INPUT)
    return at.value(_MAF) * 3.6 / (afr * at.const("const.fuel_density_kg_per_l"))
