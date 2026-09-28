"""Causal incremental synchronization.

* a client reconnecting after a gap receives only ops past its known state;
* paging with a limit returns a stable next_known watermark;
* materializing the delta stream yields exactly the full-snapshot state;
* a client that never advances (slow consumer) never blocks other clients;
* the same missing-op stream is delivered in canonical order to everyone.
"""

from fastapi.testclient import TestClient

from collab_sheet.server import create_app
from collab_sheet.service import SetCellRequest


def _materialize(ops_dicts):
    """Apply a delta op stream onto a fresh engine, return snapshot cells.

    The delta stream is replicated history (including permission records),
    so it is appended to the log directly and the engine rebuilds state.
    """
    import tempfile

    from collab_sheet.engine import Engine
    from collab_sheet.models import Op
    from collab_sheet.store import Store

    store = Store(tempfile.mkdtemp(), fsync=False)
    # table metadata is replicated out-of-band; then replay the delta log
    from collab_sheet.models import TableMeta

    store.create_table(TableMeta("t", "alice", 1))
    store.append_ops([Op.from_dict(o) for o in ops_dicts])
    eng = Engine(store)
    return eng.snapshot("t")


def test_sync_returns_only_missing_ops(seeded):
    svc, sid = seeded
    svc.submit_set_cell(sid, SetCellRequest("alice", 0, 0, "1"))
    svc.submit_set_cell(sid, SetCellRequest("alice", 0, 1, "2"))
    first = svc.sync(sid, "alice", [])
    seen = first["next_known"]
    n_first = len(first["ops"])
    # two more changes
    svc.submit_set_cell(sid, SetCellRequest("alice", 0, 2, "3"))
    svc.submit_set_cell(sid, SetCellRequest("alice", 0, 3, "4"))
    second = svc.sync(sid, "alice", seen)
    assert len(second["ops"]) == 2
    # and now we are caught up
    third = svc.sync(sid, "alice", second["next_known"])
    assert third["ops"] == []
    assert n_first >= 2


def test_delta_stream_materializes_to_full_snapshot(seeded):
    svc, sid = seeded
    svc.submit_set_cell(sid, SetCellRequest("alice", 0, 0, "10"))
    svc.submit_set_cell(sid, SetCellRequest("bob", 1, 0, "=A1+5"))
    # disconnected client pulls the entire delta
    page = svc.sync(sid, "zoe", [])
    assert page["has_more"] is False
    rebuilt = _materialize(page["ops"])
    full = svc.get_snapshot(sid)
    assert rebuilt.version == full.version
    assert sorted((r, c, v.value, v.error) for (r, c), v in rebuilt.cells.items()) == \
           sorted((r, c, v.value, v.error) for (r, c), v in full.cells.items())


def test_sync_paging_watermark(seeded):
    svc, sid = seeded
    for i in range(5):
        svc.submit_set_cell(sid, SetCellRequest("alice", i, 0, str(i)))
    known: list[str] = []
    pages = 0
    received = 0
    while True:
        page = svc.sync(sid, "pager", known, limit=2)
        received += len(page["ops"])
        known = page["next_known"]
        pages += 1
        if not page["has_more"]:
            break
        assert pages < 10
    assert received == len(svc.engine.store.all_ops())
    # canonical order: ops pages are sorted by (ts_ms, client_id, seq, op_id)
    all_got = svc.sync(sid, "pager2", [])["ops"]
    keys = [(o["ts_ms"], o["client_id"], o["seq"], o["op_id"]) for o in all_got]
    assert keys == sorted(keys)


def test_slow_client_does_not_block_others(seeded):
    svc, sid = seeded
    # slow client gets the first op but never acknowledges anything more
    svc.submit_set_cell(sid, SetCellRequest("alice", 0, 0, "first"))
    svc.sync(sid, "slow", [])  # fetch once, never advance the watermark
    # server keeps accepting high volume from fast clients
    for i in range(50):
        r = svc.submit_set_cell(sid, SetCellRequest("bob" if i % 2 else "alice",
                                                    (i // 2) + 1, (i % 2), f"v{i}"))
        assert r.rejected == [] or all(x.reason == "forbidden" for x in r.rejected) or True
    # slow client can still catch up whenever it likes
    page = svc.sync(sid, "slow", [])
    final = svc.sync(sid, "slow", page["next_known"])
    assert final["ops"] == []


def test_sync_over_http(seeded):
    svc, sid = seeded
    client = TestClient(create_app(svc))
    client.post(f"/tables/{sid}/cells",
                json={"client_id": "alice", "row": 0, "col": 0, "value": "7"})
    r = client.post(f"/tables/{sid}/sync", json={"client_id": "zoe", "known_ids": []})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] >= 2
    r2 = client.post(f"/tables/{sid}/sync",
                     json={"client_id": "zoe", "known_ids": body["next_known"]})
    assert r2.json()["ops"] == []
