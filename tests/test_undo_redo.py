"""Single-client undo/redo under concurrent merges.

* undo reverts only the acting client's op, never others' later writes;
* redo restores the client's expected result, again respecting merges;
* fresh edits after an undo discard redo history (linear undo/redo);
* undo/redo ops are durable and idempotent;
* you can never undo another client's operation.
"""

from conftest import set_op, undo_op

from collab_sheet.service import BatchSubmitRequest, SetCellRequest


def test_undo_does_not_roll_back_other_client_writes(seeded):
    svc, sid = seeded
    svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("a1", "alice", 1, 100, 0, 0, "A").to_dict()]))
    svc.submit_batch(sid, BatchSubmitRequest("bob",
        [set_op("b1", "bob", 1, 200, 0, 0, "B").to_dict()]))
    assert svc.get_cell(sid, 0, 0).value == "B"
    # alice undoes her A: bob's B stays
    res = svc.undo(sid, "alice")
    assert res.accepted
    assert svc.get_cell(sid, 0, 0).value == "B"
    assert svc.get_cell(sid, 0, 0).provenance["client_id"] == "bob"


def test_undo_then_revert_exposes_previous_value_when_still_winning(seeded):
    svc, sid = seeded
    svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("a1", "alice", 1, 100, 3, 3, "one").to_dict(),
         set_op("a2", "alice", 2, 110, 3, 3, "two").to_dict()]))
    assert svc.get_cell(sid, 3, 3).value == "two"
    svc.undo(sid, "alice")
    assert svc.get_cell(sid, 3, 3).value == "one"
    svc.undo(sid, "alice")
    assert svc.get_cell(sid, 3, 3) is None
    # redo walks forward again
    svc.redo(sid, "alice")
    assert svc.get_cell(sid, 3, 3).value == "one"
    svc.redo(sid, "alice")
    assert svc.get_cell(sid, 3, 3).value == "two"


def test_redo_after_concurrent_edit_keeps_merge_semantics(seeded):
    svc, sid = seeded
    svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("a1", "alice", 1, 100, 0, 0, "A").to_dict()]))
    svc.undo(sid, "alice")
    assert svc.get_cell(sid, 0, 0) is None
    # bob writes the same cell while alice's op is undone
    svc.submit_batch(sid, BatchSubmitRequest("bob",
        [set_op("b1", "bob", 1, 300, 0, 0, "B").to_dict()]))
    # alice redoes: her A is live again but loses deterministically to B
    res = svc.redo(sid, "alice")
    assert res.accepted
    assert svc.get_cell(sid, 0, 0).value == "B"
    # a *newer* write from alice wins again through the normal merge rule
    svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("a9", "alice", 9, 900, 0, 0, "A-new").to_dict()]))
    assert svc.get_cell(sid, 0, 0).value == "A-new"


def test_new_edit_after_undo_clears_redo_history(seeded):
    svc, sid = seeded
    svc.submit_set_cell(sid, SetCellRequest("alice", 0, 0, "first"))
    svc.undo(sid, "alice")
    assert svc.get_cell(sid, 0, 0) is None
    # a brand new edit invalidates redo
    svc.submit_set_cell(sid, SetCellRequest("alice", 0, 0, "second"))
    res = svc.redo(sid, "alice")
    assert res.accepted == []
    assert svc.get_cell(sid, 0, 0).value == "second"


def test_cannot_undo_other_clients_op(seeded):
    svc, sid = seeded
    svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("a1", "alice", 1, 100, 0, 0, "A").to_dict()]))
    # bob's stack is empty anyway; force a targeted undo op: rejected
    res = svc.engine.commit([undo_op("evil", "bob", 1, 200, "a1")])
    assert res.rejected and res.rejected[0].reason == "forbidden"
    assert svc.get_cell(sid, 0, 0).value == "A"


def test_undo_survives_restart(seeded):
    svc, sid = seeded
    data_dir = svc.engine.store.data_dir
    svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("a1", "alice", 1, 100, 0, 0, "A").to_dict()]))
    svc.undo(sid, "alice")
    assert svc.get_cell(sid, 0, 0) is None
    # simulate restart: recover from disk with the same clock-independent history
    from conftest import FakeClock

    from collab_sheet.service import build_service

    svc2 = build_service(data_dir, fsync=False, clock=FakeClock())
    assert svc2.get_cell(sid, 0, 0) is None
    res = svc2.redo(sid, "alice")
    assert res.accepted
    assert svc2.get_cell(sid, 0, 0).value == "A"


def test_undo_replay_is_idempotent(seeded):
    svc, sid = seeded
    svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("a1", "alice", 1, 100, 0, 0, "A").to_dict()]))
    u = undo_op("u1", "alice", 2, 200, "a1")
    r1 = svc.engine.commit([u])
    version = r1.version
    r2 = svc.engine.commit([u])
    assert r2.accepted == ["u1"] and r2.rejected == []
    assert r2.version == version
    assert svc.get_cell(sid, 0, 0) is None
