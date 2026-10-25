"""Generic CAN frame packer/unpacker driven by the generated defs tables.

``moto_defs.platform`` exports the platform-bus frame IDs, lengths and E2E
DataIDs (``MESSAGES``) and the per-signal layout (``SIGNALS``: start bit, length,
byte order, signedness, factor, offset, unit, value tables), so nothing is
hand-written or parsed here (platform rule #2). This module is a thin adapter
over those tables. The E2E CRC and alive counter are written by
``moto_defs.e2e.protect``.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from functools import cache

# Submodules are not imported by ``moto_defs/__init__``; load them explicitly.
platform = importlib.import_module("moto_defs.platform")
e2e = importlib.import_module("moto_defs.e2e")


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
    """Platform-bus messages adapted from ``moto_defs.platform`` ``MESSAGES``/``SIGNALS``."""
    result: dict[str, Message] = {}
    for name, info in platform.MESSAGES.items():
        signals: dict[str, Signal] = {}
        for g in platform.SIGNALS[name]:
            if g.byte_order != "little_endian":
                raise ValueError(f"big-endian signal {g.name} is not supported")
            signals[g.name] = Signal(
                name=g.name,
                start=g.start,
                length=g.length,
                signed=g.is_signed,
                factor=float(g.factor),
                offset=float(g.offset),
                unit=g.unit or "",
                choices={label: raw for raw, label in (g.choices or {}).items()},
            )
        result[name] = Message(name, info["frame_id"], info["length"], info["e2e_data_id"], signals)
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
