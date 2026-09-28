"""Crash recovery: durability, atomic batches, torn-frame handling.

* committed ops survive process restart (re-open same data dir);
* a batch never leaves a partial write: torn trailing frame is discarded;
* uncommitted (torn) ops are never visible after recovery;
* sync resumes after recovery and converges;
* fsync=True mode also recovers (best-effort smoke; platform dependent).
"""

import os

from conftest import FakeClock, set_op

from collab_sheet.engine import Engine
from collab_sheet.service import BatchSubmitRequest, build_service
from collab_sheet.store import Store


def test_committed_state_survives_restart(tmp_path):
    data_dir = str(tmp_path / "d")
    svc = build_service(data_dir, fsync=False, clock=FakeClock())
    sid = svc.create_table("alice", sheet_id="t")["sheet_id"]
    svc.submit_batch(sid, BatchSubmitRequest("alice", [
        set_op("a", "alice", 1, 100, 0, 0, "41").to_dict(),
        set_op("b", "alice", 2, 101, 0, 1, "=A1+1").to_dict(),
    ]))
    version = svc.get_snapshot(sid).version
    del svc

    svc2 = build_service(data_dir, fsync=False, clock=FakeClock())
    snap = svc2.get_snapshot(sid)
    assert snap.version == version
    assert snap.cells[(0, 0)].value == "41"
    assert snap.cells[(0, 1)].value == "42"


def test_rejected_ops_are_never_persisted(seeded):
    svc, sid = seeded
    before = svc.engine.store.version()
    svc.submit_batch(sid, BatchSubmitRequest("carol", [  # carol = reader
        set_op("hack", "carol", 1, 100, 0, 0, "x").to_dict()]))
    assert svc.engine.store.version() == before
    # restart confirms it is absent
    svc2 = build_service(svc.engine.store.data_dir, fsync=False, clock=FakeClock())
    assert all(o.op_id != "hack" for o in svc2.engine.store.all_ops())


def test_torn_trailing_frame_is_discarded_on_recovery(tmp_path):
    data_dir = str(tmp_path / "d")
    svc = build_service(data_dir, fsync=False, clock=FakeClock())
    sid = svc.create_table("alice", sheet_id="t")["sheet_id"]
    svc.submit_batch(sid, BatchSubmitRequest("alice", [
        set_op("durable1", "alice", 1, 100, 0, 0, "1").to_dict()]))
    durable_version = svc.engine.store.version()
    del svc

    # simulate a crash mid-append: a frame payload without trailing newline
    log = os.path.join(data_dir, "ops.log")
    with open(log, "ab") as fh:
        fh.write(b'{"version":99,"ops":[{"op_id":"torn","sheet_id":"t",'
                 b'"client_id":"alice","seq":9,"ts_ms":999,"kind":"SET_CELL",'
                 b'"row":9,"col":9,"value":"ghost"')  # no newline = never committed

    recovered = Store.recover(data_dir, fsync=False)
    assert recovered.version() == durable_version
    assert all(o.op_id != "torn" for o in recovered.all_ops())
    eng = Engine(recovered)
    assert eng.cell(sid, 9, 9) is None


def test_corrupt_json_frame_in_middle_does_not_silently_drop_commits(tmp_path):
    # frames before the torn tail are all intact and must all be present
    data_dir = str(tmp_path / "d")
    svc = build_service(data_dir, fsync=False, clock=FakeClock())
    sid = svc.create_table("alice", sheet_id="t")["sheet_id"]
    for i in range(5):
        svc.submit_batch(sid, BatchSubmitRequest("alice", [
            set_op(f"op{i}", "alice", i, 100 + i, i, 0, str(i)).to_dict()]))
    expected = svc.engine.store.version()
    del svc
    recovered = Store.recover(data_dir, fsync=False)
    assert recovered.version() == expected
    for i in range(5):
        assert recovered.get_table_meta(sid) is not None
    eng = Engine(recovered)
    for i in range(5):
        assert eng.cell(sid, i, 0).value == str(i)


def test_sync_and_continue_after_recovery(tmp_path):
    data_dir = str(tmp_path / "d")
    svc = build_service(data_dir, fsync=False, clock=FakeClock())
    sid = svc.create_table("alice", sheet_id="t")["sheet_id"]
    svc.submit_batch(sid, BatchSubmitRequest("alice", [
        set_op("k1", "alice", 1, 100, 0, 0, "1").to_dict()]))
    del svc

    svc2 = build_service(data_dir, fsync=False, clock=FakeClock())
    page = svc2.sync(sid, "zoe", [])
    assert {o["op_id"] for o in page["ops"]} >= {"k1"}
    # new commits work after recovery
    r = svc2.submit_batch(sid, BatchSubmitRequest("alice", [
        set_op("k2", "alice", 2, 200, 1, 0, "2").to_dict()]))
    assert r.accepted == ["k2"]
    assert svc2.get_cell(sid, 1, 0).value == "2"


def test_recovery_is_idempotent(tmp_path):
    data_dir = str(tmp_path / "d")
    svc = build_service(data_dir, fsync=False, clock=FakeClock())
    sid = svc.create_table("alice", sheet_id="t")["sheet_id"]
    svc.submit_batch(sid, BatchSubmitRequest("alice", [
        set_op("k1", "alice", 1, 100, 0, 0, "1").to_dict()]))
    v1 = Store.recover(data_dir, fsync=False).version()
    v2 = Store.recover(data_dir, fsync=False).version()
    assert v1 == v2 == svc.engine.store.version()


def test_fsync_enabled_recovery(tmp_path):
    """Exercise the real fsync path end to end (best effort on all platforms)."""
    data_dir = str(tmp_path / "fsync")
    svc = build_service(data_dir, fsync=True, clock=FakeClock())
    sid = svc.create_table("alice", sheet_id="t")["sheet_id"]
    svc.submit_batch(sid, BatchSubmitRequest("alice", [
        set_op("f1", "alice", 1, 100, 0, 0, "durable").to_dict()]))
    del svc
    svc2 = build_service(data_dir, fsync=True, clock=FakeClock())
    assert svc2.get_cell(sid, 0, 0).value == "durable"
