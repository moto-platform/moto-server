"""The one position-name rule of the server (D-076, invariant 7).

`GET /sessions/{id}/series`, `GET /sessions/{id}/stats` and the MDF4 export all use
``is_position_name`` so the read API and the export cannot drift apart. Kept isolated
for review.
"""

from __future__ import annotations

import re

_HORIZONTAL_TOKENS = frozenset({"lat", "latitude", "lon", "lng", "longitude", "ecef"})
_VERTICAL_TOKENS = frozenset({"alt", "altitude", "height", "hmsl"})
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def is_position_name(name: str) -> bool:
    """Whether a series or column name looks like a GPS position.

    A horizontal token anywhere in the name refuses it. A vertical token does too
    (``gps_altitude_m``), unless the name ends in ``change``: defs'
    ``derived.altitude_change`` is the barometric change since key-on, not a position.
    camelCase is split too (``gpsLat``), and the unsplit name is kept so ``hMSL`` still
    reads as ``hmsl``. "pos"/"position" are not tokens: they name non-GPS signals such as
    ``throttle_pos_pct``.
    """
    plain = [token for token in _TOKEN_SPLIT.split(name.lower()) if token]
    split = [token for token in _TOKEN_SPLIT.split(_CAMEL_BOUNDARY.sub("_", name).lower()) if token]
    tokens = set(plain) | set(split)
    if tokens & _HORIZONTAL_TOKENS:
        return True
    relative = bool(plain) and plain[-1] == "change"
    return bool(tokens & _VERTICAL_TOKENS) and not relative
