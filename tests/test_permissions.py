"""Read-only / per-cell permissions and their ordering vs concurrent edits."""

from __future__ import annotations

from .conftest import grant_op, raw_of, readonly_op, revoke_op, set_op


def test_unauthorized_write_rejected_and_not_recorded(tmp_service):
    svc = tmp_service
    result = svc.submit("s1", "c1", [set_op("c1", 1, "A1", "x", ts=5)], "b1")
    assert result.accepted == []
    assert len(result.rejected) == 1
    assert "may not write" in result.rejected[0][1]
    # rejection leaves the canonical history untouched
    assert svc._engines["s1"].state.ops == []
    assert "A1" not in svc.snapshot("s1")["cells"]
    # rejected op is not delivered to syncing clients
    assert svc.sync("s1", __import__("spreadsheet.sync", fromlist=["VersionVector"]).VersionVector.empty()) == []


def test_grant_then_write_succeeds(tmp_service):
    svc = tmp_service
    svc.submit("s1", "owner", [grant_op("owner", 1, "c1", "A1", ts=1)], "g")
    r = svc.submit("s1", "c1", [set_op("c1", 1, "A1", "ok", ts=5)], "b1")
    assert not r.rejected
    assert raw_of(svc, "s1", "A1") == "ok"


def test_revoke_blocks_later_write_but_keeps_earlier(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [
            grant_op("owner", 1, "c1", "A1", ts=1),
            revoke_op("owner", 2, "c1", "A1", ts=8),
        ],
        "perm",
    )
    earlier = svc.submit("s1", "c1", [set_op("c1", 1, "A1", "kept", ts=5)], "b1")
    later = svc.submit("s1", "c1", [set_op("c1", 2, "A1", "blocked", ts=9)], "b2")
    assert not earlier.rejected and earlier.accepted
    assert later.accepted == [] and len(later.rejected) == 1
    assert raw_of(svc, "s1", "A1") == "kept"


def test_sheet_readonly_blocks_non_owner_everywhere(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [
            grant_op("owner", 1, "c1", "A1", ts=1),
            grant_op("owner", 2, "c1", "B1", ts=2),
            readonly_op("owner", 3, True, ts=5),
        ],
        "perm",
    )
    r = svc.submit(
        "s1",
        "c1",
        [
            set_op("c1", 1, "A1", "a", ts=6),
            set_op("c1", 2, "B1", "b", ts=7),
        ],
        "writes",
    )
    assert r.accepted == [] and len(r.rejected) == 2
    # owner is still allowed even while sheet is read-only
    own = svc.submit("s1", "owner", [set_op("owner", 4, "A1", "boss", ts=8)], "own")
    assert not own.rejected
    assert raw_of(svc, "s1", "A1") == "boss"
    # unlocking again restores grants
    svc.submit("s1", "owner", [readonly_op("owner", 5, False, ts=9)], "unlock")
    r2 = svc.submit("s1", "c1", [set_op("c1", 3, "A1", "again", ts=10)], "w2")
    assert not r2.rejected
    assert raw_of(svc, "s1", "A1") == "again"


def test_non_owner_cannot_change_permissions(tmp_service):
    svc = tmp_service
    svc.submit("s1", "owner", [grant_op("owner", 1, "c1", "A1", ts=1)], "g")
    r = svc.submit("s1", "c1", [readonly_op("c1", 1, True, ts=5)], "evil")
    assert r.accepted == []
    assert r.rejected and "owner" in r.rejected[0][1]
    assert svc.snapshot("s1")["readonly"] is False


def test_permission_order_vs_writes_is_deterministic(tmp_path):
    """Admission follows causal order and converges under redelivery.

    A write with timestamp smaller than the grant is rejected regardless of
    arrival order (it causally happened before access was granted). A write
    with timestamp larger than the grant succeeds as soon as the grant has
    been observed; if it arrived early it got 403, and the client redelivers
    the *same* op (same client/seq), which is de-duplicated and then admitted.
    Final committed state is independent of the original arrival order.
    """
    import itertools

    from spreadsheet.service import SpreadsheetService
    from spreadsheet.storage import WALStorage

    grant = [grant_op("owner", 1, "c1", "A1", ts=10)]
    old = [set_op("c1", 1, "A1", "old", ts=5)]    # before grant -> always denied
    new = [set_op("c1", 2, "A1", "new", ts=20)]   # after grant -> converges in
    batches = {"g": ("owner", grant), "old": ("c1", old), "new": ("c1", new)}
    outcomes = set()
    for i, perm in enumerate(itertools.permutations(batches)):
        svc = SpreadsheetService(WALStorage(tmp_path / f"p{i}"))
        svc.create_sheet("s1", "owner")
        rejected_once: dict[str, list] = {}
        for name in perm:
            cid, ops = batches[name]
            r = svc.submit("s1", cid, ops, name)
            if not r.accepted and name != "old":
                rejected_once[name] = ops
        # clients redeliver rejected ops (same client/seq) after observing
        # the grant; grant is present by now in every permutation.
        for name, ops in rejected_once.items():
            cid, _ = batches[name]
            r = svc.submit("s1", cid, ops, f"{name}-retry")
            assert not r.rejected
        outcomes.add(raw_of(svc, "s1", "A1"))
    assert outcomes == {"new"}, outcomes
    # the pre-grant write never becomes accepted, even via retry after grant
    svc = SpreadsheetService(WALStorage(tmp_path / "old-check"))
    svc.create_sheet("s1", "owner")
    r1 = svc.submit("s1", "c1", old, "old-early")
    assert not r1.accepted
    svc.submit("s1", "owner", grant, "g")
    r2 = svc.submit("s1", "c1", old, "old-retry")
    assert not r2.accepted  # ts=5 still predates the grant
    assert raw_of(svc, "s1", "A1") is None
