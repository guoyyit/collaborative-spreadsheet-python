"""Causal incremental sync based on version vectors."""

from __future__ import annotations

from spreadsheet.sync import VersionVector, vector_from_ops

from .conftest import grant_op, set_op


def _ids(ops):
    return {(o.id.client_id, o.id.seq) for o in ops}


def test_fresh_client_gets_full_log_in_causal_order(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [set_op("owner", 1, "A1", "1", ts=1), set_op("owner", 2, "B1", "=A1", ts=2)],
        "b0",
    )
    ops = svc.sync("s1", VersionVector.empty())
    assert _ids(ops) == {("owner", 1), ("owner", 2)}
    assert [o.id.seq for o in ops] == [1, 2]


def test_reconnect_receives_only_missing_ops(tmp_service):
    svc = tmp_service
    svc.submit("s1", "owner", [set_op("owner", 1, "A1", "1", ts=1)], "b1")
    svc.submit("s1", "owner", [set_op("owner", 2, "A2", "2", ts=2)], "b2")
    known = VersionVector({"owner": 1})
    delta = svc.sync("s1", known)
    assert _ids(delta) == {("owner", 2)}
    # idempotent: asking again with the advanced vector yields nothing new
    assert svc.sync("s1", VersionVector({"owner": 2})) == []


def test_incremental_catchup_equals_full_sync(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [
            grant_op("owner", 1, "c1", "A1", ts=1),
            grant_op("owner", 2, "c1", "A2", ts=2),
        ],
        "g",
    )
    svc.submit("s1", "c1", [set_op("c1", 1, "A1", "x", ts=5)], "c1a")
    # client last saw owner up to 1 and nothing from c1
    known = VersionVector({"owner": 1})
    # simulate several poll rounds while more ops keep arriving
    rounds = []
    rounds.append(svc.sync("s1", known))
    svc.submit("s1", "c1", [set_op("c1", 2, "A2", "y", ts=7)], "c1b")
    svc.submit("s1", "owner", [set_op("owner", 3, "A3", "z", ts=8)], "oa")
    seen = set(_ids(rounds[0]))
    cur = vector_from_ops(rounds[0]).merge(known)
    rounds.append(svc.sync("s1", cur))
    seen |= _ids(rounds[1])
    # final received set plus what the client already knew == one full sync
    known_ids = {
        (c, s)
        for c, bound in known.clocks.items()
        for s in range(1, bound + 1)
    }
    full = svc.sync("s1", VersionVector.empty())
    assert seen | known_ids == _ids(full)
    assert svc.sync("s1", svc.version_vector("s1")) == []


def test_slow_client_does_not_block_others(tmp_service):
    """Service operations stay independent: a lagging vector never gates others."""
    import time
    svc = tmp_service
    svc.submit("s1", "owner", [set_op("owner", 1, "A1", "1", ts=1)], "b1")
    # a very stale client and a fresh one can poll concurrently; answers are
    # computed independently from the immutable canonical log
    stale = VersionVector.empty()
    t0 = time.perf_counter()
    d_stale = svc.sync("s1", stale)
    d_fresh = svc.sync("s1", VersionVector({"owner": 1}))
    elapsed = time.perf_counter() - t0
    assert len(d_stale) >= 1
    assert d_fresh == []
    assert elapsed < 2.0
