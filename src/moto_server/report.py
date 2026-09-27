"""Builds the per-session validation report (report.json) and its text rendering.

Re-derives everything from raw_hex / *_raw columns (never trusts the app's own
decoded columns as ground truth) and cross-checks the two, per the ingest
contract: "raw_hex is authoritative; the server re-decodes it with the schema
and ignores the app's decoded columns except for a consistency check."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from moto_server import ble_schema, signals
from moto_server.decode import decode_imu_csv, decode_telemetry_csv
from moto_server.defs import defs_version

# rx_mono_ms / device_time_ms gaps larger than this multiple of notifyPeriodMs
# are reported (schema rule referenced by the ingest contract).
GAP_MULTIPLIER = 3

# A device_time_ms decrease is treated as the documented uint32 wraparound
# (~49.7 days) only if the previous value was already near the top of the
# range and the wrapped-around delta is itself plausible; anything else is a
# reboot/reset event, reported separately from ordering violations.
_UINT32_MAX = 2**32
_WRAP_NEAR_MAX_MARGIN = 1_000_000


def build_report(
    session_dir: Path,
    session_id: str,
    meta: dict[str, Any],
    summary: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = schema or ble_schema.load_default_schema()
    gap_threshold_ms = schema["notifyPeriodMs"] * GAP_MULTIPLIER

    layout, rows = decode_telemetry_csv(session_dir / "telemetry.csv", schema)

    packet_versions: dict[int, int] = {}
    decode_error_rows: list[int] = []
    mismatch_rows: list[int] = []
    schema_versions_seen: set[int] = set()
    for row in rows:
        packet_versions[row.app_packet_version] = packet_versions.get(row.app_packet_version, 0) + 1
        if row.decoded is not None:
            schema_versions_seen.add(row.decoded["version"])
        if row.decode_error:
            decode_error_rows.append(row.row_index)
        if row.mismatches:
            mismatch_rows.append(row.row_index)

    packet_count = len(rows)
    lost_total = 0
    ordering_violations: list[int] = []
    device_time_resets: list[dict[str, Any]] = []
    rx_gaps: list[dict[str, Any]] = []
    device_time_gaps: list[dict[str, Any]] = []

    prev = None
    for row in rows:
        if prev is not None:
            lost_total += (row.seq - prev.seq - 1) % 256
            if row.rx_mono_ms < prev.rx_mono_ms:
                ordering_violations.append(row.row_index)
            elif row.rx_mono_ms - prev.rx_mono_ms > gap_threshold_ms:
                rx_gaps.append({"row": row.row_index, "gap_ms": row.rx_mono_ms - prev.rx_mono_ms})

            curr_dt = row.decoded["device_time_ms"] if row.decoded else None
            prev_dt = prev.decoded["device_time_ms"] if prev.decoded else None
            if curr_dt is not None and prev_dt is not None:
                if curr_dt < prev_dt:
                    wrap_delta = (curr_dt - prev_dt) % _UINT32_MAX
                    is_plausible_wrap = (
                        prev_dt > _UINT32_MAX - _WRAP_NEAR_MAX_MARGIN
                        and wrap_delta < gap_threshold_ms * 10
                    )
                    if not is_plausible_wrap:
                        device_time_resets.append(
                            {"row": row.row_index, "prev": prev_dt, "curr": curr_dt}
                        )
                elif curr_dt - prev_dt > gap_threshold_ms:
                    device_time_gaps.append({"row": row.row_index, "gap_ms": curr_dt - prev_dt})
        prev = row

    loss_percent = (
        lost_total / (lost_total + packet_count) * 100 if (lost_total + packet_count) else 0.0
    )

    range_summary: dict[str, dict[str, Any]] = {}
    valid_summary: dict[str, dict[str, Any]] = {}
    for defs_signal in signals.known_defs_signals(schema):
        lo, hi = signals.signal_range(defs_signal)
        flag_name = signals.flag_name_for(defs_signal)
        key = signals.signal_key(defs_signal)
        total_count = 0
        valid_count = 0
        out_of_range = 0
        for row in rows:
            if row.decoded is None:
                continue
            value = row.decoded["physical"].get(defs_signal)
            if value is None:
                continue
            total_count += 1
            is_valid = bool(row.decoded["flags"].get(flag_name)) if flag_name else True
            if is_valid:
                valid_count += 1
                if not (lo <= value <= hi):
                    out_of_range += 1
        range_summary[key] = {
            "checked_count": valid_count,
            "out_of_range_count": out_of_range,
            "out_of_range_percent": (out_of_range / valid_count * 100) if valid_count else 0.0,
        }
        valid_summary[key] = {
            "total_count": total_count,
            "valid_count": valid_count,
            "valid_percent": (valid_count / total_count * 100) if total_count else 0.0,
        }

    can_rows = [row for row in rows if row.decoded and row.decoded["can_health"]]
    can_health_summary = None
    if can_rows:
        bus_states_seen = sorted(
            {
                row.decoded["can_health"]["bus_state"]
                for row in can_rows
                if row.decoded["can_health"]["bus_state"]
            }
        )
        can_health_summary = {
            "max_tec": max(row.decoded["can_health"]["tec"] for row in can_rows),
            "max_rec": max(row.decoded["can_health"]["rec"] for row in can_rows),
            "max_bus_off_count": max(
                row.decoded["can_health"]["bus_off_count"] for row in can_rows
            ),
            "max_unanswered_did_count": max(
                row.decoded["can_health"]["unanswered_did_count"] for row in can_rows
            ),
            "latched_foreign_tester": any(
                row.decoded["can_health"]["flags"].get("latchedForeignTester") for row in can_rows
            ),
            "latched_bus_off": any(
                row.decoded["can_health"]["flags"].get("latchedBusOff") for row in can_rows
            ),
            "bus_states_seen": bus_states_seen,
        }

    imu_summary = None
    imu_csv = session_dir / "imu.csv"
    if imu_csv.exists():
        imu_rows = decode_imu_csv(imu_csv, schema)
        missing_total = 0
        prev_sample_index = None
        for imu_row in imu_rows:
            if prev_sample_index is not None:
                missing_total += (imu_row.sample_index - prev_sample_index - 1) % 65536
            prev_sample_index = imu_row.sample_index
        scale_mismatch_rows = [r.row_index for r in imu_rows if r.mismatches]
        duration_s = None
        if len(imu_rows) >= 2:
            duration_s = (
                (imu_rows[-1].device_time_ms - imu_rows[0].device_time_ms) % _UINT32_MAX
            ) / 1000.0
        effective_rate_hz = (len(imu_rows) / duration_s) if duration_s else None
        total_expected = missing_total + len(imu_rows)

        # block_flags is repeated across every sample row of the same block
        # (one BLE notification per block); count each block once, by its
        # block_seq transition, per imuBlock.flags in the schema.
        imu_flag_bits = schema["imuBlock"]["flags"]["bits"]
        block_count = 0
        device_overflow_blocks = 0
        read_error_blocks = 0
        sensor_reconfigured_blocks = 0
        prev_block_seq = None
        for imu_row in imu_rows:
            if imu_row.block_seq == prev_block_seq:
                continue
            prev_block_seq = imu_row.block_seq
            block_count += 1
            block_flags = ble_schema.decode_flags(imu_row.block_flags, imu_flag_bits)
            device_overflow_blocks += bool(block_flags.get("deviceOverflow"))
            read_error_blocks += bool(block_flags.get("readError"))
            sensor_reconfigured_blocks += bool(block_flags.get("sensorReconfigured"))

        imu_summary = {
            "sample_count": len(imu_rows),
            "missing_samples": missing_total,
            "loss_percent": (missing_total / total_expected * 100) if total_expected else 0.0,
            "effective_rate_hz": effective_rate_hz,
            "scale_mismatch_rows": scale_mismatch_rows,
            "block_count": block_count,
            "blocks_with_device_overflow": device_overflow_blocks,
            "blocks_with_read_error": read_error_blocks,
            "blocks_with_sensor_reconfigured": sensor_reconfigured_blocks,
        }

    status = "ok"
    reasons: list[str] = []

    def warn(reason: str) -> None:
        nonlocal status
        if status == "ok":
            status = "warn"
        reasons.append(reason)

    if decode_error_rows:
        status = "fail"
        reasons.append(f"{len(decode_error_rows)} telemetry row(s) failed to decode from raw_hex")
    if any(item["out_of_range_count"] for item in range_summary.values()):
        warn("one or more signals had out-of-range values")
    if loss_percent > 1.0:
        warn(f"packet loss {loss_percent:.2f}% > 1%")
    if mismatch_rows:
        warn(f"{len(mismatch_rows)} row(s) disagree with the app's own decoded columns")
    if ordering_violations:
        warn(f"{len(ordering_violations)} rx_mono_ms ordering violation(s)")
    if device_time_resets:
        warn(f"{len(device_time_resets)} device_time_ms reset/reboot event(s)")
    if imu_summary and imu_summary["missing_samples"]:
        warn(f"{imu_summary['missing_samples']} missing IMU sample(s)")
    if imu_summary and imu_summary["blocks_with_sensor_reconfigured"]:
        warn(
            f"{imu_summary['blocks_with_sensor_reconfigured']} IMU block(s) after a sensor "
            "reconfiguration (samples before may be stale or wrong-scale)"
        )
    if not reasons:
        reasons.append("no issues found")

    return {
        "session_id": session_id,
        "defs_version": defs_version(),
        "app_version": meta.get("app_version"),
        "ble_schema_version_meta": meta.get("ble_schema_version"),
        "telemetry_layout": layout,
        "schema_versions_seen": sorted(schema_versions_seen),
        "packet_versions": {str(k): v for k, v in sorted(packet_versions.items())},
        "packet_count": packet_count,
        "status": status,
        "reasons": reasons,
        "loss": {"lost_count": lost_total, "loss_percent": loss_percent},
        "consistency": {"decode_error_rows": decode_error_rows, "mismatch_rows": mismatch_rows},
        "ordering": {
            "rx_mono_ms_violations": ordering_violations,
            "device_time_ms_resets": device_time_resets,
        },
        "gaps": {
            "rx_mono_ms_gaps": rx_gaps,
            "device_time_ms_gaps": device_time_gaps,
            "threshold_ms": gap_threshold_ms,
        },
        "range_checks": range_summary,
        "valid_ratios": valid_summary,
        "can_health": can_health_summary,
        "imu": imu_summary,
        "summary_json": summary,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"Session {report['session_id']} -- status: {report['status'].upper()}",
        f"  defs version: {report['defs_version']}  app version: {report['app_version']}",
        f"  telemetry layout: {report['telemetry_layout']}  "
        f"packet versions seen: {report['packet_versions']}",
        f"  packets: {report['packet_count']}  lost: {report['loss']['lost_count']} "
        f"({report['loss']['loss_percent']:.2f}%)",
        "  reasons:",
    ]
    lines += [f"    - {reason}" for reason in report["reasons"]]

    if report["consistency"]["mismatch_rows"]:
        lines.append(
            f"  app/server decode mismatches: {len(report['consistency']['mismatch_rows'])} row(s)"
        )
    if report["ordering"]["rx_mono_ms_violations"]:
        lines.append(
            f"  rx_mono_ms ordering violations: {len(report['ordering']['rx_mono_ms_violations'])}"
        )
    if report["ordering"]["device_time_ms_resets"]:
        lines.append(f"  device_time_ms resets: {len(report['ordering']['device_time_ms_resets'])}")
    if report["gaps"]["rx_mono_ms_gaps"]:
        lines.append(
            f"  rx_mono_ms gaps (> {report['gaps']['threshold_ms']} ms): "
            f"{len(report['gaps']['rx_mono_ms_gaps'])}"
        )

    lines.append("  signal ranges:")
    for key, item in report["range_checks"].items():
        valid = report["valid_ratios"][key]
        lines.append(
            f"    {key}: valid {valid['valid_percent']:.1f}% of {valid['total_count']}, "
            f"out-of-range {item['out_of_range_count']} ({item['out_of_range_percent']:.2f}%)"
        )

    if report["can_health"]:
        can = report["can_health"]
        lines.append(
            f"  CAN health: max TEC={can['max_tec']} max REC={can['max_rec']} "
            f"bus-off={can['max_bus_off_count']} unanswered DIDs={can['max_unanswered_did_count']} "
            f"latched(foreign_tester={can['latched_foreign_tester']}, "
            f"bus_off={can['latched_bus_off']}) states={can['bus_states_seen']}"
        )

    if report["imu"]:
        imu = report["imu"]
        rate = f"{imu['effective_rate_hz']:.1f} Hz" if imu["effective_rate_hz"] else "n/a"
        lines.append(
            f"  IMU: {imu['sample_count']} samples in {imu['block_count']} block(s), "
            f"{imu['missing_samples']} missing ({imu['loss_percent']:.2f}%), "
            f"effective rate {rate}, scale mismatches {len(imu['scale_mismatch_rows'])}"
        )
        if imu["blocks_with_sensor_reconfigured"]:
            lines.append(
                f"    sensor reconfigured: {imu['blocks_with_sensor_reconfigured']} block(s)"
            )
        if imu["blocks_with_device_overflow"] or imu["blocks_with_read_error"]:
            lines.append(
                f"    device overflow: {imu['blocks_with_device_overflow']} block(s), "
                f"read error: {imu['blocks_with_read_error']} block(s)"
            )

    return "\n".join(lines) + "\n"
