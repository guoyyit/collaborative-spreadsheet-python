"""Durable write-ahead-log persistence.

On-disk layout under ``data_dir``::

    meta.jsonl          one JSON object per CREATE_TABLE
    ops.log             one *frame* per committed batch, newline terminated

A frame is a single JSON line::

    {"version": 12, "ops": [ {..Op..}, ... ]}

Atomicity
---------
A batch is serialized to one line and appended with a single ``write``
followed by ``flush``/``fsync`` and the terminating newline is part of the
same write.  On recovery, a final line without a newline (a torn trailing
write from a crash mid-append) is discarded - those ops were never committed
and never become visible.

Idempotency
-----------
``op_id`` is the global dedup key: re-delivering the same op body is acked
silently.  A reused id with a *different* body raises :class:`DuplicateOp`.
"""

import json
import os
import threading
from collections.abc import Iterable

from .models import Op, TableMeta


class DuplicateOp(Exception):
    """An op id was reused with a different op body."""

    def __init__(self, op_id: str) -> None:
        super().__init__(f"op_id {op_id!r} reused with a different body")
        self.op_id = op_id


def _op_identity(op: Op) -> tuple:
    """Fields that must match when the same op_id is redelivered."""
    return (
        op.sheet_id, op.client_id, op.seq, op.ts_ms, op.kind,
        op.row, op.col, op.value, op.scope, op.target, op.role,
        op.grantee, op.undone_id, op.redone_id, op.toggled,
    )


class Store:
    """File-backed store. All public methods are guarded by one lock."""

    def __init__(self, data_dir: str, fsync: bool = True) -> None:
        self.data_dir = data_dir
        self.fsync = fsync
        os.makedirs(data_dir, exist_ok=True)
        self._meta_path = os.path.join(data_dir, "meta.jsonl")
        self._log_path = os.path.join(data_dir, "ops.log")
        self._lock = threading.RLock()
        self._tables: dict[str, TableMeta] = {}
        self._ops: list[Op] = []
        self._index: dict[str, int] = {}  # op_id -> position in _ops
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        if os.path.exists(self._meta_path):
            with open(self._meta_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    meta = TableMeta(d["sheet_id"], d["owner_id"], d.get("created_ms", 0))
                    self._tables[meta.sheet_id] = meta
        if os.path.exists(self._log_path):
            with open(self._log_path, "rb") as fh:
                raw = fh.read()
            lines = raw.split(b"\n")
            # last element after trailing newline is b""; a non-empty final
            # element means a torn frame -> drop it.
            if lines and lines[-1] != b"":
                lines = lines[:-1]
            for line in lines:
                if not line.strip():
                    continue
                frame = json.loads(line.decode("utf-8"))
                for od in frame["ops"]:
                    op = Op.from_dict(od)
                    if op.op_id in self._index:
                        continue
                    self._index[op.op_id] = len(self._ops)
                    self._ops.append(op)

    # ---------------------------------------------------------------- tables
    def create_table(self, meta: TableMeta) -> None:
        with self._lock:
            if meta.sheet_id in self._tables:
                return
            self._tables[meta.sheet_id] = meta
            self._append_line(
                self._meta_path,
                json.dumps(
                    {"sheet_id": meta.sheet_id, "owner_id": meta.owner_id, "created_ms": meta.created_ms},
                    separators=(",", ":"),
                ),
            )

    def get_table_meta(self, sheet_id: str) -> TableMeta | None:
        with self._lock:
            return self._tables.get(sheet_id)

    def list_tables(self) -> list[TableMeta]:
        with self._lock:
            return list(self._tables.values())

    # ------------------------------------------------------------------ ops
    def append_ops(self, ops: Iterable[Op]) -> tuple[list[Op], int]:
        """Atomically append a batch. Returns (newly_committed_ops, version).

        Ops whose id is already present with an identical body are skipped.
        A conflicting id raises :class:`DuplicateOp` and nothing is written.
        """
        batch = list(ops)
        with self._lock:
            fresh: list[Op] = []
            for op in batch:
                if op.op_id in self._index:
                    existing = self._ops[self._index[op.op_id]]
                    if _op_identity(existing) != _op_identity(op):
                        raise DuplicateOp(op.op_id)
                    continue
                # duplicate within the same batch
                if any(o.op_id == op.op_id for o in fresh):
                    continue
                fresh.append(op)
            if fresh:
                frame = {
                    "version": len(self._ops) + len(fresh),
                    "ops": [o.to_dict() for o in fresh],
                }
                self._append_line(self._log_path, json.dumps(frame, separators=(",", ":")))
                for op in fresh:
                    self._index[op.op_id] = len(self._ops)
                    self._ops.append(op)
            return fresh, len(self._ops)

    def all_ops(self) -> list[Op]:
        with self._lock:
            return list(self._ops)

    def version(self) -> int:
        with self._lock:
            return len(self._ops)

    # -------------------------------------------------------------- helpers
    def _append_line(self, path: str, line: str) -> None:
        payload = line + "\n"
        with open(path, "ab") as fh:
            fh.write(payload.encode("utf-8"))
            fh.flush()
            if self.fsync:
                os.fsync(fh.fileno())

    @staticmethod
    def recover(data_dir: str, fsync: bool = True) -> "Store":
        """Construct a store, replaying the WAL and dropping torn frames."""
        return Store(data_dir, fsync=fsync)
