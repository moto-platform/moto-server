"""can_codec adapter over the generated moto_defs.platform tables."""

from __future__ import annotations

import pytest

from moto_server.can_codec import pack_raw, platform, platform_messages, unpack_raw


def test_every_platform_message_is_adapted():
    msgs = platform_messages()
    assert set(msgs) == set(platform.MESSAGES)
    for name, msg in msgs.items():
        table = platform.SIGNALS[name]
        assert list(msg.signals) == [s.name for s in table]
        for s in table:
            got = msg.signals[s.name]
            assert (got.start, got.length, got.signed) == (s.start, s.length, s.is_signed)
            assert (got.factor, got.offset, got.unit) == (s.factor, s.offset, s.unit or "")
            # table is value -> name; the adapter exposes name -> value
            assert got.choices == {n: v for v, n in (s.choices or {}).items()}


@pytest.mark.parametrize("name", sorted(platform.MESSAGES))
def test_pack_unpack_roundtrip_all_signals(name):
    msg = platform_messages()[name]
    raws = {}
    for sig in msg.signals.values():
        hi = (1 << (sig.length - 1)) - 1 if sig.signed else (1 << sig.length) - 1
        raws[sig.name] = hi if sig.length > 1 else 1
    payload = pack_raw(msg, raws)
    assert len(payload) == msg.length
    assert unpack_raw(msg, bytes(payload)) == raws
    neg = {s.name: -1 for s in msg.signals.values() if s.signed}
    if neg:
        assert unpack_raw(msg, bytes(pack_raw(msg, neg))) == {n: neg.get(n, 0) for n in msg.signals}
