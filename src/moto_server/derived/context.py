"""What a formula sees: the input feeds of one derived signal, sampled at one instant.

Sampling rule (D-070 item 2): an input's value at time t is its last sample at or before
t, and it only counts while its age (t - sample time) is within the input's own
stale_after_ms. A stale input raises InputInvalid, which the engine turns into a null
output with a flag; a held value is never presented as fresh. A derived input is a feed
too: its grid points are samples whose value is None when that output was invalid.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from moto_server.derived.inputs import constant

FLAG_STALE_INPUT = "stale_input"
FLAG_INVALID_INPUT = "invalid_input"
FLAG_INSUFFICIENT_SAMPLES = "insufficient_samples"
FLAG_OUT_OF_RANGE = "out_of_range"


class InputInvalid(Exception):
    """An input is unusable at this instant; the output is null with `flag`."""

    def __init__(self, flag: str):
        super().__init__(flag)
        self.flag = flag


@dataclass(frozen=True)
class Feed:
    """Samples of one input (times ascending); value None = an invalid derived output."""

    times: list[int]
    values: list[float | None]
    stale_after_ms: int

    def latest(self, t: int) -> int:
        """Index of the last sample at or before t, -1 when there is none."""
        return bisect_right(self.times, t) - 1


class Env:
    """The declared inputs of one signal: feeds by input name, plus its window."""

    def __init__(self, inputs: list[str], feeds: dict[str, Feed], window_ms: int | None):
        self.inputs = inputs
        self.feeds = feeds
        self.window_ms = window_ms

    def feed(self, name: str) -> Feed:
        if name not in self.inputs:
            raise KeyError(f"{name!r} is not a declared input (declared: {self.inputs})")
        return self.feeds[name]

    def const(self, name: str) -> float:
        if name not in self.inputs or not name.startswith("const."):
            raise KeyError(f"{name!r} is not a declared constant input")
        return constant(name.removeprefix("const."))


class At:
    """Input access of one signal at one instant t."""

    def __init__(self, env: Env, t: int):
        self.env = env
        self.t = t

    def _fresh_index(self, name: str) -> tuple[Feed, int]:
        feed = self.env.feed(name)
        i = feed.latest(self.t)
        if i < 0 or self.t - feed.times[i] > feed.stale_after_ms:
            raise InputInvalid(FLAG_STALE_INPUT)
        return feed, i

    def value(self, name: str) -> float:
        feed, i = self._fresh_index(name)
        value = feed.values[i]
        if value is None:
            raise InputInvalid(FLAG_INVALID_INPUT)
        return value

    def window(self, name: str) -> list[tuple[int, float]]:
        """Valid samples in (t - window_ms, t]; the newest sample at or before t must be fresh."""
        feed, i = self._fresh_index(name)
        if feed.values[i] is None:
            raise InputInvalid(FLAG_INVALID_INPUT)
        if self.env.window_ms is None:
            raise KeyError("this signal has no window_ms")
        lo = bisect_right(feed.times, self.t - self.env.window_ms)
        return [
            (feed.times[j], feed.values[j]) for j in range(lo, i + 1) if feed.values[j] is not None
        ]

    def const(self, name: str) -> float:
        return self.env.const(name)
