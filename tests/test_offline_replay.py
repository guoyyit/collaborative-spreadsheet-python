"""Offline editing and delayed replay.

Rules under test:
* offline ops replay in occurrence (client seq) order;
* a stale offline write (older Lamport timestamp) must NOT overwrite a
  concurrently committed write by another client;
* edits to untouched cells still merge in;
* duplicate batch and duplicate operation delivery are idempotent.
"""

from __future__ import annotations

import pytest

from spreadsheet.errors import DuplicateSubmitError

from .conftest import Clock, grant_op, raw_of, set_op


def test_offline_replay_keeps_order_and_does_not_overwrite(tmp_service):
    svc = tmp_service
    clk = Clock()
    svc.submit(
        "s1",
        "owner",
        [
            grant_op("owner", 1, "c1", "A1", ts=clk.tick()),
            grant_op("owner", 2, "c1", "C1", ts=clk.tick()),
        ],
        "grants",
    )
    # common state visible to everyone at logical time 5
    svc.submit("s1", "owner", [set_op("owner", 3, "A1", "base", ts=5)], "base")

    # c1 goes offline. Concurrently the owner edits A1 at logical time 10.
    svc.submit("s1", "owner", [set_op("owner", 4, "A1", "online-new", ts=10)], "online")
    svc.submit("s1", "owner", [set_op("owner", 5, "B1", "online-b", ts=11)], "online-b")

    # c1's offline edits happened at 6/7: stale on A1, fresh on C1.
    offline = [
        set_op("c1", 1, "A1", "offline-a1", ts=6),
        set_op("c1", 2, "C1", "offline-c1", ts=7),
    ]
    result = svc.submit("s1", "c1", offline, "offline-batch")
    assert not result.rejected

    cells = svc.snapshot("s1")["cells"]
    assert cells["A1"]["raw"] == "online-new"  # concurrent work not lost
    assert cells["B1"]["raw"] == "online-b"
    assert cells["C1"]["raw"] == "offline-c1"  # untouched cell merged
    # attribution preserves the true winner
    assert cells["A1"]["written_by"] == "owner:4"
    assert cells["C1"]["written_by"] == "c1:2"


def test_offline_replay_is_occurrence_ordered(tmp_service):
    svc = tmp_service
    clk = Clock()
    svc.submit("s1", "owner", [grant_op("owner", 1, "c1", "A1", ts=clk.tick())], "g")
    # deliver ops out of order in the request list; must still commit in seq
    ops_shuffled = [
        set_op("c1", 3, "A1", "third", ts=8),
        set_op("c1", 1, "A1", "first", ts=6),
        set_op("c1", 2, "A1", "second", ts=7),
    ]
    svc.submit("s1", "c1", ops_shuffled, "offline")
    assert raw_of(svc, "s1", "A1") == "third"


def test_duplicate_batch_delivery_is_idempotent(tmp_service):
    svc = tmp_service
    ops = [set_op("owner", 1, "A1", "x", ts=1)]
    svc.submit("s1", "owner", ops, "b1")
    with pytest.raises(DuplicateSubmitError):
        svc.submit("s1", "owner", ops, "b1")
    assert len(svc._engines["s1"].state.ops) == 1
    assert raw_of(svc, "s1", "A1") == "x"


def test_duplicate_operation_redelivery_applies_once(tmp_service):
    svc = tmp_service
    op = set_op("owner", 1, "A1", "once", ts=1)
    svc.submit("s1", "owner", [op], "b1")
    # same logical op resent inside a *new* batch (retry with new batch id)
    svc.submit("s1", "owner", [op], "b2")
    engine = svc._engines["s1"]
    a1_ops = [o for o in engine.state.ops if o.cell is not None and o.cell.format() == "A1"]
    assert len(a1_ops) == 1
    assert raw_of(svc, "s1", "A1") == "once"


def test_replay_after_restart_preserves_history(tmp_path):
    from spreadsheet.service import SpreadsheetService
    from spreadsheet.storage import WALStorage

    d = tmp_path / "data"
    svc = SpreadsheetService(WALStorage(d))
    svc.create_sheet("s1", "owner")
    svc.submit("s1", "owner", [set_op("owner", 1, "A1", "durable", ts=1)], "b1")
    svc2 = SpreadsheetService(WALStorage(d))
    assert raw_of(svc2, "s1", "A1") == "durable"
    # late offline replay after restart merges normally
    svc2.submit("s1", "owner", [set_op("owner", 2, "B1", "late", ts=5)], "b2")
    assert raw_of(svc2, "s1", "B1") == "late"
