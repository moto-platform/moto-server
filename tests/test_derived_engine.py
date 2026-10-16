"""The derived-signal engine: ordering, staleness, readiness, range and grid rules."""

from __future__ import annotations

import pytest

from moto_server.defs import vehicle_derived
from moto_server.derived import engine
from moto_server.derived.context import At
from moto_server.derived.formulas import Formula

from .derived_helpers import FakeInputs, const_series, ramp


def _spec(inputs: list[str], **kw):
    return {
        "class": "A",
        "unit": "u",
        "range": [-100.0, 100.0],
        "inputs": inputs,
        "period_ms": 100,
        "stale_after_ms": 250,
        "ready_phase0": True,
        **kw,
    }


def _point(fn):
    return Formula("point", fn)


def _by_name(evaluation: engine.Evaluation) -> dict[str, engine.SignalOutput]:
    return {o.name: o for o in evaluation.outputs}


# --- dependency order ------------------------------------------------------------


def test_dependency_order_puts_every_derived_input_first_in_the_real_defs():
    order = engine.dependency_order(vehicle_derived.SIGNALS)
    assert set(order) == set(vehicle_derived.SIGNALS)
    position = {name: i for i, name in enumerate(order)}
    for name, spec in vehicle_derived.SIGNALS.items():
        for inp in spec["inputs"]:
            if inp.startswith("derived."):
                assert position[inp.removeprefix("derived.")] < position[name], (name, inp)


def test_dependency_cycle_is_rejected():
    specs = {"a": _spec(["derived.b"]), "b": _spec(["derived.a"])}
    with pytest.raises(ValueError, match="cycle"):
        engine.dependency_order(specs)


def test_a_consumer_listed_before_its_producer_still_sees_the_producer():
    specs = {
        "third": _spec(["derived.second"]),
        "second": _spec(["derived.first"]),
        "first": _spec(["did.X"]),
    }
    formulas = {
        "first": _point(lambda at: at.value("did.X") + 1.0),
        "second": _point(lambda at: at.value("derived.first") * 2.0),
        "third": _point(lambda at: at.value("derived.second") + 10.0),
    }
    result = engine.evaluate(
        FakeInputs({"did.X": const_series(0, 500, 50, 1.0)}), specs, formulas, {}
    )
    assert [o.name for o in result.outputs] == ["first", "second", "third"]
    assert set(_by_name(result)["third"].values) == {14.0}


# --- staleness -------------------------------------------------------------------


def test_stale_input_gives_null_not_a_held_value():
    # speed: 0..200 ms, then nothing until 1000..1200 ms; stale limit of the signal 250 ms
    times = [0, 100, 200, 1000, 1100, 1200]
    specs = {"s": _spec(["did.X"])}
    formulas = {"s": _point(lambda at: at.value("did.X"))}
    result = engine.evaluate(FakeInputs({"did.X": (times, [5.0] * 6)}), specs, formulas, {})
    out = _by_name(result)["s"]
    by_time = dict(zip(out.times, zip(out.values, out.flags, strict=True), strict=True))
    assert by_time[200] == (5.0, None)
    assert by_time[400] == (5.0, None)  # age 200 <= 250
    assert by_time[500] == (None, "stale_input")  # age 300 > 250
    assert by_time[900] == (None, "stale_input")
    assert by_time[1000] == (5.0, None)


def test_the_stale_limit_of_a_sensor_comes_from_sensors_not_the_signal():
    specs = {"s": _spec(["sensor.x"], stale_after_ms=10_000)}
    formulas = {"s": _point(lambda at: at.value("sensor.x"))}
    sensors = {"x": {"stale_after_ms": 30}}
    result = engine.evaluate(
        FakeInputs({"sensor.x": ([0, 200], [1.0, 1.0])}), specs, formulas, sensors
    )
    out = _by_name(result)["s"]
    assert dict(zip(out.times, out.values, strict=True)) == {
        0: 1.0,
        100: None,
        200: 1.0,
    }


def test_an_invalid_derived_input_makes_the_consumer_invalid():
    # engine_torque is out of range (50 % of 100 Nm = 50 Nm > 30 Nm) -> torque_gap is null
    inputs = {
        "did.ACTUAL_TORQUE_PCT": const_series(0, 4000, 500, 50.0),
        "did.DEMAND_TORQUE_PCT": const_series(0, 4000, 500, 25.0),
        "did.REFERENCE_TORQUE": const_series(0, 4000, 500, 100.0),
    }
    result = engine.evaluate(FakeInputs(inputs))
    out = _by_name(result)
    assert set(out["engine_torque"].flags) == {"out_of_range"}
    assert set(out["torque_gap"].flags) == {"invalid_input"}
    assert result.coverage["torque_gap"]["valid_rows"] == 0


def test_gear_estimate_follows_the_invalid_ratio_at_standstill():
    inputs = {
        "did.ENGINE_SPEED": const_series(0, 1000, 100, 1500.0),
        "did.VEHICLE_SPEED": const_series(0, 1000, 100, 0.0),
    }
    out = _by_name(engine.evaluate(FakeInputs(inputs)))
    assert set(out["gearbox_ratio_measured"].flags) == {"standstill"}
    assert set(out["gear_estimate"].flags) == {"invalid_input"}


# --- readiness, class, events ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "reason"),
    [("engine_vibration_rms", "ready_phase0"), ("friction_torque", "ready_phase0")],
)
def test_signals_that_are_not_ready_are_never_evaluated(name: str, reason: str):
    spec = vehicle_derived.SIGNALS[name]
    assert not spec["ready_phase0"] and spec["pending_inputs"]
    # every input present: still no row, whatever formulas say
    inputs = {
        inp: const_series(0, 5000, 10, 1.0)
        for inp in spec["inputs"]
        if inp.startswith(("did.", "sensor."))
    }
    formulas = {name: _point(lambda at: 1.0)}
    result = engine.evaluate(FakeInputs(inputs), formulas=formulas)
    assert name not in _by_name(result)
    assert result.coverage[name]["status"] == engine.NOT_READY
    assert result.coverage[name]["reason"] == reason
    assert result.coverage[name]["pending_inputs"] == spec["pending_inputs"]


def test_pending_inputs_alone_make_a_signal_not_ready():
    specs = {"p": _spec(["did.X"], pending_inputs=["something undefined"])}
    result = engine.evaluate(
        FakeInputs({"did.X": const_series(0, 500, 50, 1.0)}),
        specs,
        {"p": _point(lambda at: 1.0)},
        {},
    )
    assert result.outputs == []
    assert result.coverage["p"]["status"] == engine.NOT_READY
    assert result.coverage["p"]["reason"] == "pending_inputs"


def test_other_classes_events_and_unimplemented_signals_are_not_implemented():
    result = engine.evaluate(FakeInputs({}))
    coverage = result.coverage
    assert coverage["speedometer_error"] == {
        "class": "B",
        "status": engine.NOT_IMPLEMENTED,
        "reason": "class B",
    }
    assert coverage["ride_distance"]["status"] == engine.NOT_IMPLEMENTED
    assert coverage["stop_event"]["reason"] == "event"
    assert coverage["clutch_state"]["reason"] == "no formula"
    assert set(coverage) == set(vehicle_derived.SIGNALS)


# --- range -----------------------------------------------------------------------


def test_out_of_range_result_is_null_with_a_flag():
    specs = {"r": _spec(["did.X"], range=[0.0, 10.0])}
    formulas = {"r": _point(lambda at: at.value("did.X"))}
    values = [5.0, 11.0, -1.0, 10.0, float("nan")]
    result = engine.evaluate(
        FakeInputs({"did.X": ([0, 100, 200, 300, 400], values)}), specs, formulas, {}
    )
    out = _by_name(result)["r"]
    assert out.values == [5.0, None, None, 10.0, None]
    assert out.flags == [None, "out_of_range", "out_of_range", None, "out_of_range"]
    assert result.coverage["r"]["flag_counts"] == {"out_of_range": 3}
    assert result.coverage["r"]["valid_rows"] == 2


# --- grid ------------------------------------------------------------------------


def test_the_output_grid_uses_the_period_from_the_defs():
    inputs = {
        "did.ENGINE_SPEED": const_series(0, 5000, 50, 3000.0),
        "did.VEHICLE_SPEED": const_series(0, 5000, 50, 36.0),
    }
    result = engine.evaluate(FakeInputs(inputs))
    out = _by_name(result)
    for name in ("gearbox_ratio_measured", "trip_distance"):
        period = vehicle_derived.SIGNALS[name]["period_ms"]
        times = out[name].times
        assert {b - a for a, b in zip(times, times[1:], strict=False)} == {period}
    assert out["gearbox_ratio_measured"].times[0] == 0
    assert out["gearbox_ratio_measured"].times[-1] == 4950  # last grid point <= end (5000)


def test_the_grid_spans_the_overlap_of_the_inputs():
    specs = {"s": _spec(["did.A", "did.B"], period_ms=100)}
    formulas = {"s": _point(lambda at: at.value("did.A") + at.value("did.B"))}
    inputs = {"did.A": const_series(100, 600, 50, 1.0), "did.B": const_series(300, 900, 50, 2.0)}
    out = _by_name(engine.evaluate(FakeInputs(inputs), specs, formulas, {}))["s"]
    assert out.times == [300, 400, 500, 600]
    assert set(out.values) == {3.0}


def test_inputs_without_a_common_span_are_missing_inputs():
    specs = {"s": _spec(["did.A", "did.B"])}
    formulas = {"s": _point(lambda at: 0.0)}
    inputs = {"did.A": const_series(0, 100, 50, 1.0), "did.B": const_series(900, 1000, 50, 2.0)}
    result = engine.evaluate(FakeInputs(inputs), specs, formulas, {})
    assert result.coverage["s"]["status"] == engine.MISSING_INPUTS
    assert result.coverage["s"]["reason"] == "no common time span"


# --- missing inputs and errors ---------------------------------------------------


def test_absent_inputs_are_listed_and_propagate_to_dependants():
    inputs = {"did.ENGINE_SPEED": const_series(0, 1000, 100, 3000.0)}
    result = engine.evaluate(FakeInputs(inputs))
    ratio = result.coverage["gearbox_ratio_measured"]
    assert ratio["status"] == engine.MISSING_INPUTS
    assert ratio["missing"] == ["did.VEHICLE_SPEED"]
    assert result.coverage["gear_estimate"]["missing"] == ["derived.gearbox_ratio_measured"]
    assert result.coverage["trip_time"]["status"] == engine.COMPUTED


def test_a_failing_formula_is_an_error_and_does_not_stop_the_others():
    def boom(at: At) -> float:
        raise RuntimeError("bad formula")

    specs = {"bad": _spec(["did.X"]), "good": _spec(["did.X"])}
    formulas = {"bad": _point(boom), "good": _point(lambda at: 1.0)}
    result = engine.evaluate(
        FakeInputs({"did.X": const_series(0, 500, 50, 1.0)}), specs, formulas, {}
    )
    assert result.coverage["bad"] == {
        "class": "A",
        "status": engine.ERROR,
        "message": "RuntimeError: bad formula",
    }
    assert result.coverage["good"]["status"] == engine.COMPUTED


def test_provisional_constants_are_reported():
    inputs = {
        "did.ENGINE_SPEED": const_series(0, 1000, 100, 3000.0),
        "did.VEHICLE_SPEED": ramp(0, 1000, 100, lambda t: 30.0),
    }
    entry = engine.evaluate(FakeInputs(inputs)).coverage["gearbox_ratio_measured"]
    assert entry["provisional_constants"] == ["rear_wheel_radius_dynamic_m"]
