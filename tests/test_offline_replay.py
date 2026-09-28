"""Offline editing and delayed replay.

* offline ops replay in the client's occurrence order and all land;
* duplicate delivery (same op_id) applies exactly once;
* replay never wipes concurrent committed writes from other clients;
* offline client's own causal ts wins/loses by the normal merge rule;
* same set of ops converges whether replay arrives before or after others.
"""

import itertools
import tempfile

from conftest import perm_op, set_op

from collab_sheet.engine import Engine
from collab_sheet.service import BatchSubmitRequest
from collab_sheet.store import Store


def test_offline_batch_replays_in_order(seeded):
    svc, sid = seeded
    # bob goes offline; prepares 3 edits with strictly increasing causal ts/seq
    offline = [
        set_op("off1", "bob", 10, 1000, 0, 0, "1"),
        set_op("off2", "bob", 11, 1001, 1, 0, "=A1+1"),
        set_op("off3", "bob", 12, 1002, 2, 0, "=A2*10"),
    ]
    # meanwhile alice commits online (ts before bob's offline edits)
    svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("on1", "alice", 1, 500, 0, 0, "41").to_dict()]))
    # bob reconnects and replays in occurrence order
    res = svc.submit_batch(sid, BatchSubmitRequest("bob", [o.to_dict() for o in offline]))
    assert res.rejected == []
    assert res.accepted == ["off1", "off2", "off3"]
    assert svc.get_cell(sid, 0, 0).value == "1"      # bob's offline op wins (ts 1000)
    assert svc.get_cell(sid, 1, 0).value == "2"      # A2 = A1+1
    assert svc.get_cell(sid, 2, 0).value == "20"     # A3 = A2*10


def test_replay_duplicate_deliveries_apply_once(seeded):
    svc, sid = seeded
    payload = [set_op("d1", "bob", 1, 100, 0, 0, "X").to_dict()]
    first = svc.submit_batch(sid, BatchSubmitRequest("bob", payload))
    version_after = first.version
    for _ in range(5):  # network retries
        again = svc.submit_batch(sid, BatchSubmitRequest("bob", payload))
        assert again.accepted == ["d1"]
        assert again.rejected == []
        assert again.version == version_after
    assert svc.get_snapshot(sid).version == version_after


def test_replay_does_not_overwrite_concurrent_commits(seeded):
    svc, sid = seeded
    # bob offline edit has LOW causal ts (he was offline "earlier")
    offline = set_op("bob-off", "bob", 1, 100, 0, 0, "offline-value")
    # alice commits a newer write while bob is away
    svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("alice-new", "alice", 2, 900, 0, 0, "online-value").to_dict()]))
    res = svc.submit_batch(sid, BatchSubmitRequest("bob", [offline.to_dict()]))
    assert res.accepted == ["bob-off"]          # it is kept in history ...
    # ... but does not overwrite alice's newer committed value
    assert svc.get_cell(sid, 0, 0).value == "online-value"
    assert svc.get_cell(sid, 0, 0).provenance["op_id"] == "alice-new"
    # the offline op is still traceable in the log
    assert any(o.op_id == "bob-off" for o in svc.engine.store.all_ops())


def test_replay_arrival_order_irrelevant():
    ops = [
        set_op("n1", "alice", 1, 100, 0, 0, "A"),
        set_op("n2", "bob", 1, 200, 0, 1, "=A1+1"),
        set_op("n3", "bob", 2, 300, 0, 2, "B"),
    ]
    results = set()
    for perm in itertools.permutations(ops):
        eng = Engine(Store(tempfile.mkdtemp(), fsync=False))
        eng.create_table("t", "alice", 1)
        eng.commit([perm_op("gb", "alice", "bob", 1, 1, "writer")])
        for op in perm:
            eng.commit([op])
        snap = eng.snapshot("t")
        results.add(tuple((r, c, v.value, v.error) for (r, c), v in sorted(snap.cells.items())))
    assert len(results) == 1, results


def test_mixed_batch_with_one_bad_op_is_atomic(seeded):
    svc, sid = seeded
    good = set_op("ok", "bob", 1, 100, 0, 0, "fine")
    bad = set_op("nope", "carol", 1, 101, 0, 1, "denied")  # carol = reader
    res = svc.submit_batch(sid, BatchSubmitRequest("bob",
                                                  [good.to_dict(), bad.to_dict()]))
    assert {r.op_id for r in res.rejected} == {"nope"}
    assert res.accepted == []          # whole new-op batch rejected atomically
    assert svc.get_cell(sid, 0, 0) is None or svc.get_cell(sid, 0, 0).value == ""
