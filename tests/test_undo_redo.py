"""Per-client selective undo/redo in the presence of concurrent merges."""

from __future__ import annotations

import pytest

from spreadsheet.errors import PermissionDeniedError

from .conftest import grant_op, raw_of, set_op


def test_simple_undo_redo(tmp_service):
    svc = tmp_service
    r = svc.submit("s1", "owner", [set_op("owner", 1, "A1", "v1", ts=1)], "b1")
    svc.note_user_ops("s1", "owner", r.accepted)
    r2 = svc.submit("s1", "owner", [set_op("owner", 2, "A1", "v2", ts=2)], "b2")
    svc.note_user_ops("s1", "owner", r2.accepted)
    assert raw_of(svc, "s1", "A1") == "v2"
    svc.undo("s1", "owner")
    assert raw_of(svc, "s1", "A1") == "v1"
    svc.undo("s1", "owner")
    assert "A1" not in svc.snapshot("s1")["cells"]
    svc.redo("s1", "owner")
    assert raw_of(svc, "s1", "A1") == "v1"
    svc.redo("s1", "owner")
    assert raw_of(svc, "s1", "A1") == "v2"


def test_undo_does_not_roll_back_other_clients_concurrent_write(tmp_service):
    svc = tmp_service
    svc.submit("s1", "owner", [grant_op("owner", 1, "c1", "A1", ts=1)], "g")
    r1 = svc.submit("s1", "c1", [set_op("c1", 1, "A1", "c1-base", ts=5)], "b1")
    svc.note_user_ops("s1", "c1", r1.accepted)
    # c2-style concurrent overwrite by the owner after c1's edit
    svc.submit("s1", "owner", [set_op("owner", 2, "A1", "owner-wins", ts=9)], "b2")
    assert raw_of(svc, "s1", "A1") == "owner-wins"
    # c1 tries to undo its old op: winner is now the owner, so the undo must
    # be a no-op on A1 (other client's content preserved).
    op = svc.undo("s1", "c1")
    assert op is None
    assert raw_of(svc, "s1", "A1") == "owner-wins"


def test_undo_own_latest_write_restores_prior_value_not_others(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [
            set_op("owner", 1, "A1", "base", ts=1),
            set_op("owner", 2, "B1", "keep", ts=2),
            set_op("owner", 3, "A1", "mine", ts=3),
        ],
        "b",
    )
    svc.note_user_ops(
        "s1",
        "owner",
        [
            set_op("owner", 1, "A1", "base", ts=1),
            set_op("owner", 2, "B1", "keep", ts=2),
            set_op("owner", 3, "A1", "mine", ts=3),
        ],
    )
    svc.undo("s1", "owner")  # undo A1=mine -> restores base
    assert raw_of(svc, "s1", "A1") == "base"
    assert raw_of(svc, "s1", "B1") == "keep"  # unrelated cell untouched
    svc.redo("s1", "owner")
    assert raw_of(svc, "s1", "A1") == "mine"


def test_undo_rejected_when_permission_revoked(tmp_service):
    svc = tmp_service
    svc.submit("s1", "owner", [grant_op("owner", 1, "c1", "A1", ts=1)], "g")
    r = svc.submit("s1", "c1", [set_op("c1", 1, "A1", "x", ts=5)], "b1")
    svc.note_user_ops("s1", "c1", r.accepted)
    from .conftest import revoke_op
    svc.submit("s1", "owner", [revoke_op("owner", 2, "c1", "A1", ts=8)], "rev")
    with pytest.raises(PermissionDeniedError):
        svc.undo("s1", "c1")
    assert raw_of(svc, "s1", "A1") == "x"


def test_undo_stacks_are_isolated_per_client(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [grant_op("owner", 1, "c1", "A1", ts=1), grant_op("owner", 2, "c2", "B1", ts=2)],
        "g",
    )
    r1 = svc.submit("s1", "c1", [set_op("c1", 1, "A1", "a", ts=5)], "b1")
    svc.note_user_ops("s1", "c1", r1.accepted)
    r2 = svc.submit("s1", "c2", [set_op("c2", 1, "B1", "b", ts=6)], "b2")
    svc.note_user_ops("s1", "c2", r2.accepted)
    svc.undo("s1", "c1")
    assert "A1" not in svc.snapshot("s1")["cells"]
    assert raw_of(svc, "s1", "B1") == "b"  # c2 unaffected
    svc.undo("s1", "c2")
    assert "B1" not in svc.snapshot("s1")["cells"]
