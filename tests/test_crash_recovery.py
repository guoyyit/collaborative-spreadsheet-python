"""Crash recovery and atomic batch durability."""

from __future__ import annotations

import json

from spreadsheet.service import SpreadsheetService
from spreadsheet.storage import WALStorage

from .conftest import raw_of, set_op, value_of


def test_committed_state_survives_restart(tmp_path):
    d = tmp_path / "data"
    svc = SpreadsheetService(WALStorage(d))
    svc.create_sheet("s1", "owner")
    svc.submit(
        "s1",
        "owner",
        [
            set_op("owner", 1, "A1", "3", ts=1),
            set_op("owner", 2, "B1", "=A1*4", ts=2),
        ],
        "b1",
    )
    svc2 = SpreadsheetService(WALStorage(d))
    snap = svc2.snapshot("s1")
    assert snap["cells"]["A1"]["raw"] == "3"
    assert snap["cells"]["B1"]["value"] == 12
    # can keep editing and syncing after recovery
    svc2.submit("s1", "owner", [set_op("owner", 3, "A1", "5", ts=3)], "b2")
    assert value_of(svc2, "s1", "B1") == 20


def test_torn_temp_file_is_ignored_on_restart(tmp_path):
    d = tmp_path / "data"
    svc = SpreadsheetService(WALStorage(d))
    svc.create_sheet("s1", "owner")
    svc.submit("s1", "owner", [set_op("owner", 1, "A1", "ok", ts=1)], "b1")
    # crash simulation: half-written temp file left behind
    sheet_dir = d / "s1"
    (sheet_dir / ".tmp-crash.json").write_text('{"batch_id": "never", "ops": [')
    svc2 = SpreadsheetService(WALStorage(d))
    assert raw_of(svc2, "s1", "A1") == "ok"
    assert len(svc2.storage.committed_batch_ids("s1")) == 1


def test_batch_is_all_or_nothing(tmp_path):
    """A crash before the atomic rename leaves none of the batch visible."""
    d = tmp_path / "data"
    svc = SpreadsheetService(WALStorage(d))
    svc.create_sheet("s1", "owner")
    svc.submit("s1", "owner", [set_op("owner", 1, "A1", "before", ts=1)], "b0")
    sheet_dir = d / "s1"
    # Simulate a crash after fsync but before rename: a fully written temp
    # file exists but no batch-N file references it.
    payload = json.dumps(
        {
            "batch_id": "ghost",
            "ops": [set_op("owner", 2, "B1", "ghost", ts=2).to_dict()],
        }
    )
    (sheet_dir / ".tmp-ghost.json").write_text(payload)
    svc2 = SpreadsheetService(WALStorage(d))
    cells = svc2.snapshot("s1")["cells"]
    assert cells["A1"]["raw"] == "before"
    assert "B1" not in cells
    assert "ghost" not in svc2.storage.committed_batch_ids("s1")
    # and sync after recovery does not leak the ghost change
    from spreadsheet.sync import VersionVector
    ids = {(o.id.client_id, o.id.seq) for o in svc2.sync("s1", VersionVector.empty())}
    assert ids == {("owner", 1)}


def test_duplicate_batch_after_restart_does_not_double_apply(tmp_path):
    d = tmp_path / "data"
    svc = SpreadsheetService(WALStorage(d))
    svc.create_sheet("s1", "owner")
    svc.submit("s1", "owner", [set_op("owner", 1, "A1", "x", ts=1)], "b1")
    svc2 = SpreadsheetService(WALStorage(d))
    # same batch id resent by a retrying client after the server restarted
    svc2.storage.append_batch(
        "s1", "b1", [set_op("owner", 1, "A1", "x", ts=1)]
    )
    files = list((d / "s1").glob("batch-*.json"))
    assert len(files) == 1


def test_multiple_sheets_recover_independently(tmp_path):
    d = tmp_path / "data"
    svc = SpreadsheetService(WALStorage(d))
    svc.create_sheet("alpha", "oa")
    svc.create_sheet("beta", "ob")
    svc.submit("alpha", "oa", [set_op("oa", 1, "A1", "a", ts=1, sheet="alpha")], "ba")
    svc.submit("beta", "ob", [set_op("ob", 1, "A1", "b", ts=1, sheet="beta")], "bb")
    svc2 = SpreadsheetService(WALStorage(d))
    assert raw_of(svc2, "alpha", "A1") == "a"
    assert raw_of(svc2, "beta", "A1") == "b"
