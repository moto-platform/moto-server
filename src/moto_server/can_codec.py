"""Generic CAN frame packer/unpacker driven by the defs DBC tables.

``moto_defs.platform`` exports the platform-bus frame IDs, lengths and E2E
DataIDs but no per-signal layout, so the layout (start bit, length, byte order,
signedness, factor, offset, unit) is read from ``dbc/platform.dbc`` of the
pinned defs submodule (platform rule #2: nothing is hand-written here). The E2E
CRC and alive counter are written by ``moto_defs.e2e.protect``.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from moto_server.defs import moto_defs  # also puts gen/python on sys.path

# Submodules are not imported by ``moto_defs/__init__``; load them explicitly.
platform = importlib.import_module("moto_defs.platform")
e2e = importlib.import_module("moto_defs.e2e")
_DBC_PATH = Path(moto_defs.__file__).resolve().parents[3] / "dbc" / "platform.dbc"

_BO_RE = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\w+)")
_SG_RE = re.compile(
    r"^\s*SG_\s+(\w+)\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*"
    r"\(([^,]+),([^)]+)\)\s*\[([^|]+)\|([^\]]+)\]\s*\"([^\"]*)\""
)
_VAL_RE = re.compile(r"^VAL_\s+(\d+)\s+(\w+)\s+(.*);\s*$")


@dataclass(frozen=True)
class Signal:
    name: str
    start: int
    length: int
    signed: bool
    factor: float
    offset: float
    unit: str
    choices: dict[str, int] = field(default_factory=dict)

    def to_raw(self, physical: float) -> int:
        raw = round((physical - self.offset) / self.factor)
        if self.signed:
            lo, hi = -(1 << (self.length - 1)), (1 << (self.length - 1)) - 1
        else:
            lo, hi = 0, (1 << self.length) - 1
        return max(lo, min(hi, raw))

    def to_physical(self, raw: int) -> float:
        return raw * self.factor + self.offset


@dataclass(frozen=True)
class Message:
    name: str
    frame_id: int
    length: int
    e2e_data_id: int | None
    signals: dict[str, Signal]


@cache
def platform_messages() -> dict[str, Message]:
    """Platform-bus messages: IDs/E2E from ``moto_defs.platform``, layout from the DBC."""
    by_id: dict[int, tuple[str, int, dict[str, Signal]]] = {}
    pending: dict[int, dict[str, dict[str, int]]] = {}
    current: int | None = None
    raw_signals: dict[int, list[tuple]] = {}
    for line in _DBC_PATH.read_text().splitlines():
        if m := _BO_RE.match(line):
            current = int(m.group(1))
            by_id[current] = (m.group(2), int(m.group(3)), {})
            raw_signals[current] = []
        elif m := _SG_RE.match(line):
            if current is None:
                continue
            if m.group(4) != "1":
                raise ValueError(f"big-endian signal {m.group(1)} is not supported")
            raw_signals[current].append(m.groups())
        elif m := _VAL_RE.match(line):
            pairs = re.findall(r"(-?\d+)\s+\"([^\"]*)\"", m.group(3))
            pending.setdefault(int(m.group(1)), {})[m.group(2)] = {v: int(k) for k, v in pairs}
        elif not line.strip():
            current = None

    result: dict[str, Message] = {}
    for name, info in platform.MESSAGES.items():
        fid = info["frame_id"]
        dbc_name, dbc_len, _ = by_id[fid]
        if dbc_len != info["length"]:
            raise ValueError(f"{name}: DBC length {dbc_len} != defs length {info['length']}")
        signals = {
            g[0]: Signal(
                name=g[0],
                start=int(g[1]),
                length=int(g[2]),
                signed=g[4] == "-",
                factor=float(g[5]),
                offset=float(g[6]),
                unit=g[9],
                choices=pending.get(fid, {}).get(g[0], {}),
            )
            for g in raw_signals[fid]
        }
        result[name] = Message(name, fid, info["length"], info["e2e_data_id"], signals)
    return result


def pack_raw(msg: Message, raws: dict[str, int]) -> bytearray:
    """Pack raw signal values (little-endian layout) into a zeroed payload."""
    word = 0
    for name, raw in raws.items():
        sig = msg.signals[name]
        word |= (raw & ((1 << sig.length) - 1)) << sig.start
    return bytearray(word.to_bytes(msg.length, "little"))


def unpack_raw(msg: Message, data: bytes) -> dict[str, int]:
    """Unpack every signal of a payload to its raw (sign-extended) integer."""
    word = int.from_bytes(data, "little")
    out: dict[str, int] = {}
    for sig in msg.signals.values():
        raw = (word >> sig.start) & ((1 << sig.length) - 1)
        if sig.signed and raw >> (sig.length - 1):
            raw -= 1 << sig.length
        out[sig.name] = raw
    return out


def encode(
    msg: Message,
    physical: dict[str, float],
    states: dict[str, str],
    tx: e2e.TxState | None,
) -> bytes:
    """Encode physical values (and named enum states) and apply E2E when the message has it."""
    raws = {name: msg.signals[name].to_raw(value) for name, value in physical.items()}
    raws.update({name: msg.signals[name].choices[choice] for name, choice in states.items()})
    payload = pack_raw(msg, raws)
    if msg.e2e_data_id is not None:
        if tx is None:
            raise ValueError(f"{msg.name} is E2E protected and needs a TxState")
        e2e.protect(msg.e2e_data_id, payload, tx)
    return bytes(payload)
