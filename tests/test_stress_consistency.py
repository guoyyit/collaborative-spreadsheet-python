"""Additional stress / consistency scenarios."""

import tempfile
import threading

from conftest import set_op

from collab_sheet.engine import Engine
from collab_sheet.service import GrantRequest, SetCellRequest
from collab_sheet.store import Store


def test_parallel_same_cell_never_corrupts(seeded):
    svc, sid = seeded
    n = 16
    barrier = threading.Barrier(n)
    for i in range(n):
        svc.grant(sid, GrantRequest(granter_id="alice", client_id=f"c{i}", role="writer"))

    def writer(i):
        barrier.wait(timeout=10)
        for k in range(25):
            ts = 1000 + i * 1000 + k
            op = set_op(f"c{i}-{k}", f"c{i}", k, ts, 0, 0, f"{i}:{k}")
            r = svc.submit_batch(sid, type("B", (), {"client_id": f"c{i}",
                                                     "ops": [op.to_dict()]})())
            assert r.rejected == []

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
        assert not t.is_alive()

    total = n * 25
    assert svc.get_snapshot(sid).version >= total + n + 2
    # deterministic winner: highest (ts, client, seq)
    cell = svc.get_cell(sid, 0, 0)
    assert cell.provenance["client_id"] == "c15"
    assert cell.value == "15:24"

    # independent replay of exactly the same op set must agree
    from collab_sheet.models import TableMeta
    store2 = Store(tempfile.mkdtemp(), fsync=False)
    store2.create_table(TableMeta(sid, "alice", 1))
    store2.append_ops(svc.engine.store.all_ops())
    cell2 = Engine(store2).cell(sid, 0, 0)
    assert (cell2.value, cell2.provenance["op_id"]) == (cell.value, cell.provenance["op_id"])


def test_full_snapshot_equals_materialized_delta_under_concurrency(seeded):
    svc, sid = seeded

    def alice():
        for i in range(40):
            svc.submit_set_cell(sid, SetCellRequest("alice", i, 0, str(i)))

    def bob():
        for i in range(40):
            svc.submit_set_cell(sid, SetCellRequest("bob", i, 1, f"b{i}"))

    threads = [threading.Thread(target=alice), threading.Thread(target=bob)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # pull the delta stream in pages and materialize from scratch
    known: list[str] = []
    all_ops = []
    while True:
        page = svc.sync(sid, "z", known, limit=7)
        all_ops.extend(page["ops"])
        known = page["next_known"]
        if not page["has_more"]:
            break
    from collab_sheet.models import Op, TableMeta
    store = Store(tempfile.mkdtemp(), fsync=False)
    store.create_table(TableMeta(sid, "alice", 1))
    store.append_ops([Op.from_dict(o) for o in all_ops])
    rebuilt = Engine(store).snapshot(sid)
    full = svc.get_snapshot(sid)

    def vec(snap):
        return sorted((r, c, v.value, v.error) for (r, c), v in snap.cells.items())

    assert vec(rebuilt) == vec(full)
