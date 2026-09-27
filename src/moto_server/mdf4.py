"""MDF4 export -- interface skeleton only.

CLAUDE.md: "Conversion: binary -> MDF4 (archive/sharing) and Parquet (for
ML)." Parquet export is implemented (parquet.py); MDF4 (the archive/sharing
format for cross-tool exchange, e.g. Hugging Face per the phase0 plan §3.3)
is not yet.

TODO(phase0 WP-2, docs/phase0-data-collection-plan.md §3.3): implement using
the `asammdf` package (MIT licensed, reads/writes MDF3/4). Map each parquet
column to an MDF4 channel with its physical unit (see signals.py) and group
telemetry/imu into separate channel groups sharing the session's time base.
"""

from __future__ import annotations

from pathlib import Path


def export_mdf4(session_dir: Path) -> Path:
    """Exports a session's parquet data to an MDF4 file. Not implemented yet."""
    raise NotImplementedError(
        "MDF4 export is not implemented yet (see the TODO in mdf4.py); "
        "use the Parquet output under <session_dir>/parquet/ for now."
    )
