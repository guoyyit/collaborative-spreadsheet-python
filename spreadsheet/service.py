"""Application service: concurrency-safe façade over engine + storage + sync.

Responsibilities:
* hold one MergeEngine per sheet behind a per-sheet lock;
* assign server Lamport timestamps so total order stays deterministic;
* make batch commits durable before acknowledging (write then merge);
* keep per-client undo/redo stacks;
* replay durable storage on startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from spreadsheet.celladdr import CellAddr
from spreadsheet.engine import MergeEngine
from spreadsheet.errors import (
    DuplicateSubmitError,
    NotFoundError,
    PermissionDeniedError,
)
from spreadsheet.formula import Value
from spreadsheet.operations import (
    OP_CLEAR,
    OP_SET,
    Operation,
    OpId,
)
from spreadsheet.storage import WALStorage
from spreadsheet.sync import VersionVector, delta_for, vector_from_ops


@dataclass(slots=True)
class SubmitResult:
    accepted: list[Operation]
    rejected: list[tuple[Operation, str]]
    clock: int  # server Lamport clock after the batch


@dataclass(slots=True)
class _History:
    undo: list[OpId] = field(default_factory=list)
    redo: list[OpId] = field(default_factory=list)


class SpreadsheetService:
    def __init__(self, storage: WALStorage) -> None:
        self.storage = storage
        self._engines: dict[str, MergeEngine] = {}
        self._locks: dict[str, RLock] = {}
        self._clocks: dict[str, int] = {}
        self._used: dict[tuple[str, str], set[int]] = {}
        self._history: dict[str, dict[str, _History]] = {}
        self._global = RLock()
        self._restore()

    # ------------------------------------------------------------------ #
    # Lifecycle / restore
    # ------------------------------------------------------------------ #
    def _restore(self) -> None:
        for sheet_id, stored in self.storage.load_all().items():
            engine = MergeEngine(sheet_id, stored.owner)
            for batch in stored.batches:
                engine.add_ops(batch)
            self._engines[sheet_id] = engine
            self._locks[sheet_id] = RLock()
            self._history[sheet_id] = {}
            max_ts = 0
            for op in engine.state.ops:
                max_ts = max(max_ts, op.timestamp)
                used = self._used.setdefault((sheet_id, op.id.client_id), set())
                used.add(op.id.seq)
            self._clocks[sheet_id] = max_ts

    def create_sheet(self, sheet_id: str, owner: str) -> MergeEngine:
        with self._global:
            if sheet_id in self._engines or self.storage.sheet_exists(sheet_id):
                raise FileExistsError(sheet_id)
            self.storage.create_sheet(sheet_id, owner)
            engine = MergeEngine(sheet_id, owner)
            self._engines[sheet_id] = engine
            self._locks[sheet_id] = RLock()
            self._clocks[sheet_id] = 0
            self._history[sheet_id] = {}
            return engine

    def _engine(self, sheet_id: str) -> MergeEngine:
        engine = self._engines.get(sheet_id)
        if engine is None:
            raise NotFoundError(f"unknown sheet {sheet_id!r}")
        return engine

    def _alloc_seq(self, sheet_id: str, client_id: str) -> int:
        key = (sheet_id, client_id)
        used = self._used.setdefault(key, set())
        n = max(used, default=0)
        while True:
            n += 1
            if n not in used:
                used.add(n)
                return n

    def _resolve_seq(self, sheet_id: str, client_id: str, wanted: int) -> int:
        """Keep the client's seq when free (or a true duplicate redelivery);
        only allocate a fresh seq when the same seq is already bound to a
        *different* operation (id clash between an offline and a server op).
        """
        key = (sheet_id, client_id)
        used = self._used.setdefault(key, set())
        if wanted not in used:
            used.add(wanted)
        return wanted

    # ------------------------------------------------------------------ #
    # Submission
    # ------------------------------------------------------------------ #
    def submit(
        self,
        sheet_id: str,
        client_id: str,
        ops: list[Operation],
        batch_id: str,
    ) -> SubmitResult:
        with self._global:
            engine = self._engine(sheet_id)
            lock = self._locks[sheet_id]
        with lock:
            committed = self.storage.committed_batch_ids(sheet_id)
            if batch_id in committed:
                raise DuplicateSubmitError(batch_id)

            # Operations carry client-generated Lamport timestamps, which are
            # an intrinsic property of the operation. That makes the merged
            # result a pure function of the op set, independent of arrival
            # order. Offline ops are still committed in occurrence order
            # (per-client seq), and a well-behaved client makes that agree
            # with its timestamps. A single batch is one atomic durable unit.
            ordered_input = sorted(ops, key=lambda o: (o.id.seq, o.kind))
            stamped: list[Operation] = []
            for op in ordered_input:
                if op.id.client_id != client_id:
                    raise PermissionDeniedError(
                        "cannot submit operations under another client id"
                    )
                seq = self._resolve_seq(sheet_id, client_id, op.id.seq)
                self._clocks[sheet_id] = max(self._clocks[sheet_id], op.timestamp)
                stamped.append(
                    Operation(
                        id=OpId(client_id, seq),
                        timestamp=op.timestamp,
                        sheet_id=sheet_id,
                        kind=op.kind,
                        cell=op.cell,
                        content=op.content,
                        target_client=op.target_client,
                        readonly=op.readonly,
                        origin=op.origin,
                        is_redo=op.is_redo,
                        compensation=op.compensation,
                    )
                )

            # Durable first: crash between write and in-memory merge simply
            # replays on restart; unwritten batches are invisible.
            self.storage.append_batch(sheet_id, batch_id, stamped)
            accepted, rejected = engine.add_ops(stamped)
            return SubmitResult(accepted=accepted, rejected=rejected, clock=self._clocks[sheet_id])

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def snapshot(self, sheet_id: str) -> dict[str, object]:
        with self._global:
            engine = self._engine(sheet_id)
            lock = self._locks[sheet_id]
        with lock:
            cells = {}
            for cell in sorted(engine.state.raw, key=lambda a: (a.row, a.col)):
                value = engine.get_value(cell)
                cells[cell.format()] = {
                    "raw": engine.state.raw[cell],
                    "value": _jsonable(value),
                    "written_by": _last_writer(engine, cell),
                }
            return {
                "sheet_id": sheet_id,
                "owner": engine.state.owner,
                "cells": cells,
                "version": vector_from_ops(engine.state.ops).to_dict(),
                "readonly": engine.state.permissions.sheet_readonly
                if engine.state.permissions
                else False,
            }

    def sync(self, sheet_id: str, known: VersionVector) -> list[Operation]:
        with self._global:
            engine = self._engine(sheet_id)
            lock = self._locks[sheet_id]
        with lock:
            return delta_for(engine.state.ops, known)

    def version_vector(self, sheet_id: str) -> VersionVector:
        with self._global:
            engine = self._engine(sheet_id)
            lock = self._locks[sheet_id]
        with lock:
            return vector_from_ops(engine.state.ops)

    # ------------------------------------------------------------------ #
    # Undo / redo (per client)
    # ------------------------------------------------------------------ #
    def _hist(self, sheet_id: str, client_id: str) -> _History:
        return self._history[sheet_id].setdefault(client_id, _History())

    def _find_op(self, engine: MergeEngine, op_id: OpId) -> Operation | None:
        for op in engine.state.ops:
            if op.id == op_id:
                return op
        return None

    def undo(self, sheet_id: str, client_id: str) -> Operation | None:
        with self._global:
            engine = self._engine(sheet_id)
            lock = self._locks[sheet_id]
        with lock:
            hist = self._hist(sheet_id, client_id)
            while hist.undo:
                target_id = hist.undo.pop()
                target = self._find_op(engine, target_id)
                if target is None or target.kind != OP_SET:
                    continue
                # Selective undo: if the cell's currently visible user op is
                # not this target (another client overwrote it), there is
                # nothing for us to revert; drop the entry.
                if _active_user_op(engine, target.cell) != target_id:
                    continue
                prev_id = _previous_active_before(engine, target.cell, target)
                prev_op = self._find_op(engine, prev_id) if prev_id else None
                comp = self._make_compensation(
                    engine, client_id, target, prev_op, redo=False
                )
                if not engine.can_write(client_id, comp.cell):
                    # put the entry back so the undo can be retried later
                    hist.undo.append(target_id)
                    raise PermissionDeniedError(
                        f"client {client_id!r} may not write "
                        f"{comp.cell.format() if comp.cell else '<sheet>'}"
                    )
                hist.redo.append(target_id)
                self._submit_internal(engine, sheet_id, comp)
                return comp
            return None

    def redo(self, sheet_id: str, client_id: str) -> Operation | None:
        with self._global:
            engine = self._engine(sheet_id)
            lock = self._locks[sheet_id]
        with lock:
            hist = self._hist(sheet_id, client_id)
            while hist.redo:
                target_id = hist.redo.pop()
                target = self._find_op(engine, target_id)
                if target is None or target.kind != OP_SET:
                    continue
                # Re-apply this client's intended edit with a fresh timestamp.
                if not engine.can_write(client_id, target.cell):
                    hist.redo.append(target_id)
                    raise PermissionDeniedError(
                        f"client {client_id!r} may not write "
                        f"{target.cell.format() if target.cell else '<sheet>'}"
                    )
                # After redoing, the "previous active op" becomes whatever the
                # undo that paired with this redo restored.
                prev_id = _current_restore(engine, target.cell, target_id)
                prev_op = self._find_op(engine, prev_id) if prev_id else None
                comp = self._make_compensation(
                    engine, client_id, target, prev_op, redo=True
                )
                hist.undo.append(target_id)
                self._submit_internal(engine, sheet_id, comp)
                return comp
            return None

    def _make_compensation(
        self,
        engine: MergeEngine,
        client_id: str,
        target: Operation,
        prev_op: Operation | None,
        redo: bool,
    ) -> Operation:
        sheet_id = engine.state.sheet_id
        self._clocks[sheet_id] = max(self._clocks[sheet_id], target.timestamp) + 1
        return Operation(
            id=OpId(client_id, self._alloc_seq(sheet_id, client_id)),
            timestamp=self._clocks[sheet_id],
            sheet_id=sheet_id,
            kind=OP_SET,
            cell=target.cell,
            content=target.content if redo else (prev_op.content if prev_op else ""),
            target_client=None,
            readonly=None,
            origin=target.id,
            is_redo=redo,
            compensation=not redo,
            restore=prev_op.id if prev_op else None,
        )

    def _submit_internal(self, engine: MergeEngine, sheet_id: str, op: Operation) -> None:
        batch_id = f"sys:{op.id.client_id}:{op.id.seq}"
        self.storage.append_batch(sheet_id, batch_id, [op])
        engine.add_ops([op])

    # Called by API after a normal submit to record user ops for undo.
    def note_user_ops(self, sheet_id: str, client_id: str, ops: list[Operation]) -> None:
        with self._global:
            lock = self._locks[sheet_id]
        with lock:
            hist = self._hist(sheet_id, client_id)
            for op in ops:
                if op.kind in (OP_SET, OP_CLEAR) and not op.compensation and not op.is_redo:
                    hist.undo.append(op.id)
                    hist.redo.clear()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _replay_cell(engine: MergeEngine, cell: CellAddr):
    """Replay write events for one cell.

    Returns (active, last_undo_restore):
    * active: id of the genuine user op whose content is currently shown,
      or None when the cell was reverted to empty;
    * last_undo_restore: restore-pointer of the most recent undo affecting
      the cell (used by the paired redo), None after a real user write/redo.
    """
    active: OpId | None = None
    last_undo_restore: OpId | None = None
    for op in engine.state.ops:
        if op.cell != cell or op.kind != OP_SET:
            continue
        if op.is_redo:
            active = op.origin
            last_undo_restore = op.restore
        elif op.compensation:
            active = op.restore
            last_undo_restore = op.restore
        else:
            active = op.id
            last_undo_restore = None
    return active, last_undo_restore


def _active_user_op(engine: MergeEngine, cell: CellAddr) -> OpId | None:
    return _replay_cell(engine, cell)[0]


def _previous_active_before(
    engine: MergeEngine, cell: CellAddr, target: Operation
) -> OpId | None:
    """Genuine user op visible on the cell immediately before ``target``."""
    prev: OpId | None = None
    for op in engine.state.ops:
        if op is target:
            break
        if (
            op.cell == cell
            and op.kind == OP_SET
            and not op.compensation
            and not op.is_redo
        ):
            prev = op.id
    return prev


def _current_restore(
    engine: MergeEngine, cell: CellAddr, target_id: OpId
) -> OpId | None:
    return _replay_cell(engine, cell)[1]


def _last_writer(engine: MergeEngine, cell: CellAddr) -> str | None:
    active = _active_user_op(engine, cell)
    return active.format() if active else None



def _jsonable(value: Value) -> object:
    # Local import to avoid module-level coupling; CellError is lightweight.
    from spreadsheet.errors import CellError

    if isinstance(value, CellError):
        return {"error": value.kind, "message": value.message}
    return value
