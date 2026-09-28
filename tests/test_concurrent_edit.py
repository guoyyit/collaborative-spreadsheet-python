"""Concurrent edits to different regions and to the same cell.

Guarantees verified:
* concurrent batches interleave safely (single engine lock, atomic frames);
* conflicts resolve by the stable rule (ts_ms, client_id, seq, op_id);
* the same op *set* converges regardless of arrival permutation;
* every final cell exposes the winning op as provenance;
* disjoint-region concurrent edits all survive.
"""

import itertools
import tempfile
import threading

from conftest import perm_op, set_op

from collab_sheet.engine import Engine
from collab_sheet.service import BatchSubmitRequest, SetCellRequest
from collab_sheet.store import Store


def _fresh_engine_with_writers():
    eng = Engine(Store(tempfile.mkdtemp(), fsync=False))
    eng.create_table("t", "alice", 1)
    eng.commit([
        perm_op("gb", "alice", "bob", 8, 80, "writer"),
        perm_op("gc", "alice", "carol", 9, 90, "writer"),
    ])
    return eng


def test_concurrent_disjoint_regions_all_survive(seeded):
    svc, sid = seeded

    def alice():
        for i in range(20):
            r = svc.submit_set_cell(sid, SetCellRequest("alice", i, 0, f"a{i}"))
            assert r.rejected == []

    def bob():
        for i in range(20):
            r = svc.submit_set_cell(sid, SetCellRequest("bob", i, 1, f"b{i}"))
            assert r.rejected == []

    t1 = threading.Thread(target=alice)
    t2 = threading.Thread(target=bob)
    t1.start(); t2.start(); t1.join(); t2.join()

    for i in range(20):
        assert svc.get_cell(sid, i, 0).value == f"a{i}"
        assert svc.get_cell(sid, i, 1).value == f"b{i}"


def test_concurrent_same_cell_all_arrival_permutations_converge():
    ops = [
        set_op("x1", "alice", 1, 100, 2, 2, "p"),
        set_op("x2", "bob", 2, 100, 2, 2, "q"),   # tie ts: bob > alice
        set_op("x3", "carol", 1, 300, 2, 2, "r"),  # highest ts wins
    ]
    finals = set()
    provenance = set()
    for perm in itertools.permutations(ops):
        eng = _fresh_engine_with_writers()
        for op in perm:  # one-at-a-time arrival, arbitrary order
            res = eng.commit([op])
            assert res.rejected == [], [x.detail for x in res.rejected]
        cell = eng.cell("t", 2, 2)
        finals.add(cell.value)
        provenance.add(cell.provenance["op_id"])
    assert finals == {"r"}
    assert provenance == {"x3"}


def test_same_ts_tie_breaks_by_client_id_then_seq():
    o1 = set_op("t1", "alice", 1, 500, 4, 4, "from-alice")
    o2 = set_op("t2", "bob", 1, 500, 4, 4, "from-bob")
    for order in ([o1, o2], [o2, o1]):
        eng = _fresh_engine_with_writers()
        eng.commit(list(order))
        assert eng.cell("t", 4, 4).value == "from-bob"
        assert eng.cell("t", 4, 4).provenance["op_id"] == "t2"

    # same client, same ts -> higher seq wins, independent of arrival order
    s1 = set_op("s1", "alice", 1, 700, 0, 5, "v1")
    s2 = set_op("s2", "alice", 2, 700, 0, 5, "v2")
    for order in ([s1, s2], [s2, s1]):
        eng = _fresh_engine_with_writers()
        eng.commit(list(order))
        assert eng.cell("t", 0, 5).value == "v2"


def test_winner_provenance_identifies_final_author(seeded):
    svc, sid = seeded
    svc.submit_batch(sid, BatchSubmitRequest("alice", [
        set_op("a1", "alice", 1, 100, 0, 0, "A").to_dict()]))
    svc.submit_batch(sid, BatchSubmitRequest("bob", [
        set_op("b1", "bob", 1, 200, 0, 0, "B").to_dict()]))
    cell = svc.get_cell(sid, 0, 0)
    assert cell.value == "B"
    assert cell.provenance == {"op_id": "b1", "client_id": "bob", "seq": 1, "ts_ms": 200}
