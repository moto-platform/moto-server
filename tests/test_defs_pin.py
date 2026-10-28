"""`defs_pin.txt` is the fallback defs version (no git in a Docker image); it must name the pin."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vehicle_server import defs

PIN_FILE = Path(defs.__file__).resolve().parent / "defs_pin.txt"


def test_defs_pin_file_names_the_submodule_tag():
    try:
        tags = subprocess.run(
            ["git", "-C", str(defs._DEFS_ROOT), "tag", "--points-at", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("no git metadata for external/vehicle-defs")
    if not tags:
        pytest.skip("the defs checkout carries no tags (shallow clone)")
    assert PIN_FILE.read_text().strip() in tags, (
        "update src/vehicle_server/defs_pin.txt to the defs pin's release tag"
    )
