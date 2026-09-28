"""Shared fixtures and helpers for the collaborative spreadsheet test-suite."""

import threading

import pytest

from collab_sheet.models import Op
from collab_sheet.service import Clock, build_service


class FakeClock(Clock):
    """Deterministic monotonic clock (also exposed to let tests pin ts)."""

    def __init__(self, start: int = 1_000_000) -> None:
        self.t = start

    def now_ms(self) -> int:
        self.t += 1
        return self.t


@pytest.fixture
def fake_clock():
    return FakeClock()


@pytest.fixture
def svc(tmp_path, fake_clock):
    return build_service(str(tmp_path / "wal"), fsync=False, clock=fake_clock)


@pytest.fixture
def seeded(svc):
    """A table 't' owned by alice; bob=writer, carol=reader."""
    sid = svc.create_table("alice", sheet_id="t")["sheet_id"]
    from collab_sheet.service import GrantRequest

    svc.grant(sid, GrantRequest(granter_id="alice", client_id="bob", role="writer"))
    svc.grant(sid, GrantRequest(granter_id="alice", client_id="carol", role="reader"))
    return svc, sid


def set_op(op_id, client, seq, ts, row, col, value, sheet="t"):
    return Op(op_id=op_id, sheet_id=sheet, client_id=client, seq=seq, ts_ms=ts,
              kind="SET_CELL", row=row, col=col, value=value)


def perm_op(op_id, granter, grantee, seq, ts, role, target="*", scope="table", sheet="t"):
    return Op(op_id=op_id, sheet_id=sheet, client_id=granter, grantee=grantee,
              seq=seq, ts_ms=ts, kind="PERMISSION", scope=scope, target=target, role=role)


def undo_op(op_id, client, seq, ts, target, sheet="t"):
    return Op(op_id=op_id, sheet_id=sheet, client_id=client, seq=seq, ts_ms=ts,
              kind="UNDO", undone_id=target, toggled=False)


def redo_op(op_id, client, seq, ts, target, sheet="t"):
    return Op(op_id=op_id, sheet_id=sheet, client_id=client, seq=seq, ts_ms=ts,
              kind="REDO", redone_id=target, toggled=True)


def state_matrix(svc, sid, max_row=3, max_col=3):
    """Dense string matrix of displayed cell values for easy assertions."""
    snap = svc.get_snapshot(sid)
    matrix = [["" for _ in range(max_col)] for _ in range(max_row)]
    for (r, c), view in snap.cells.items():
        if r < max_row and c < max_col:
            matrix[r][c] = f"{view.value}" + (f"[{view.error}]" if view.error else "")
    return matrix


def run_concurrently(fn, *arg_lists, timeout=10):
    """Run fn(*args) in one thread per tuple in arg_lists; barrier all starts."""
    barrier = threading.Barrier(len(arg_lists))
    errors = []

    def worker(args):
        barrier.wait(timeout=timeout)
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001 - test harness surfaces any worker error
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(a,)) for a in arg_lists]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    assert not errors, errors
    assert all(not t.is_alive() for t in threads), "worker thread timed out"
