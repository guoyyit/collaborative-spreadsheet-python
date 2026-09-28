"""Application service facade.

The HTTP layer and tests both talk to :class:`SpreadsheetService`, which owns
op-id generation, per-client causal sequencing for server-side undo/redo,
and request validation, delegating merge/persistence to :class:`Engine`.
"""

import itertools
import uuid
from dataclasses import dataclass, field

from .config import Settings, get_settings
from .engine import Engine
from .models import (
    REDO,
    SET_CELL,
    UNDO,
    CommitResult,
    Op,
    TableSnapshot,
)
from .permissions import CELL_SCOPE, TABLE_SCOPE, cell_target
from .store import Store

_counter = itertools.count(1)


def new_op_id() -> str:
    return f"{next(_counter):x}-{uuid.uuid4().hex[:12]}"


class Clock:
    def now_ms(self) -> int:
        raise NotImplementedError


class SystemClock(Clock):
    import time

    def now_ms(self) -> int:
        return int(self.time.time() * 1000)


@dataclass
class SetCellRequest:
    client_id: str
    row: int
    col: int
    value: str
    op_id: str | None = None
    ts_ms: int | None = None
    seq: int | None = None


@dataclass
class RawOpRequest:
    """Submit a fully-formed op (offline batches use these)."""
    client_id: str = ""
    seq: int = 0
    ts_ms: int = 0
    kind: str = SET_CELL
    row: int = -1
    col: int = -1
    value: str = ""
    scope: str = ""
    target: str = ""
    role: str = ""
    grantee: str = ""
    op_id: str | None = None


@dataclass
class BatchSubmitRequest:
    client_id: str
    ops: list[dict] = field(default_factory=list)


@dataclass
class GrantRequest:
    granter_id: str
    client_id: str
    role: str
    scope: str = TABLE_SCOPE
    row: int = -1
    col: int = -1
    op_id: str | None = None
    ts_ms: int | None = None
    seq: int | None = None


class SpreadsheetService:
    def __init__(self, engine: Engine, clock: Clock | None = None,
                 settings: Settings | None = None) -> None:
        self.engine = engine
        self.clock = clock or SystemClock()
        self.settings = settings or get_settings()

    # -------------------------------------------------------------- tables
    def create_table(self, owner_id: str, sheet_id: str | None = None) -> dict:
        sheet_id = sheet_id or f"sheet-{uuid.uuid4().hex[:12]}"
        meta = self.engine.create_table(sheet_id, owner_id, self.clock.now_ms())
        return {"sheet_id": meta.sheet_id, "owner_id": meta.owner_id,
                "created_ms": meta.created_ms}

    # ------------------------------------------------------------- writes
    def submit_set_cell(self, sheet_id: str, req: SetCellRequest) -> CommitResult:
        op = Op(
            op_id=req.op_id or new_op_id(),
            sheet_id=sheet_id,
            client_id=req.client_id,
            seq=req.seq if req.seq is not None else self._next_seq(sheet_id, req.client_id),
            ts_ms=req.ts_ms if req.ts_ms is not None else self.clock.now_ms(),
            kind=SET_CELL,
            row=req.row,
            col=req.col,
            value=req.value,
        )
        return self.engine.commit([op])

    def submit_raw(self, sheet_id: str, req: RawOpRequest) -> CommitResult:
        op = self._raw_to_op(sheet_id, req)
        return self.engine.commit([op])

    def submit_batch(self, sheet_id: str, req: BatchSubmitRequest) -> CommitResult:
        """Submit client-formed ops (the offline-replay path).

        Each item accepts the same fields as :class:`RawOpRequest`.  ``ts_ms``
        is preserved from the client so causal order survives replay; missing
        ids are assigned, and the batch is committed in one atomic frame.
        """
        ops = [self._dict_to_op(sheet_id, req.client_id, item) for item in req.ops]
        return self.engine.commit(ops)

    def grant(self, sheet_id: str, req: GrantRequest) -> CommitResult:
        target = "*" if req.scope == TABLE_SCOPE else cell_target(req.row, req.col)
        op = Op(
            op_id=req.op_id or new_op_id(),
            sheet_id=sheet_id,
            client_id=req.granter_id,
            grantee=req.client_id,
            seq=req.seq if req.seq is not None else self._next_seq(sheet_id, req.granter_id),
            ts_ms=req.ts_ms if req.ts_ms is not None else self.clock.now_ms(),
            kind="PERMISSION",
            scope=req.scope,
            target=target,
            role=req.role,
        )
        return self.engine.commit([op])

    # -------------------------------------------------------- undo / redo
    def undo(self, sheet_id: str, client_id: str) -> CommitResult:
        targets = self.engine.undo_targets(sheet_id, client_id)
        if not targets:
            return CommitResult(accepted=[], rejected=[], version=self.engine.store.version())
        target_id = targets[-1]
        op = Op(
            op_id=new_op_id(), sheet_id=sheet_id, client_id=client_id,
            seq=self._next_seq(sheet_id, client_id),
            ts_ms=self._causal_ts(sheet_id, client_id),
            kind=UNDO, undone_id=target_id, toggled=False,
        )
        return self.engine.commit([op])

    def redo(self, sheet_id: str, client_id: str) -> CommitResult:
        targets = self.engine.redo_targets(sheet_id, client_id)
        if not targets:
            return CommitResult(accepted=[], rejected=[], version=self.engine.store.version())
        target_id = targets[-1]
        op = Op(
            op_id=new_op_id(), sheet_id=sheet_id, client_id=client_id,
            seq=self._next_seq(sheet_id, client_id),
            ts_ms=self._causal_ts(sheet_id, client_id),
            kind=REDO, redone_id=target_id, toggled=True,
        )
        return self.engine.commit([op])

    # ---------------------------------------------------------------- reads
    def get_snapshot(self, sheet_id: str) -> TableSnapshot | None:
        return self.engine.snapshot(sheet_id)

    def get_cell(self, sheet_id: str, row: int, col: int):
        return self.engine.cell(sheet_id, row, col)

    def sync(self, sheet_id: str, client_id: str, known_ids: list[str],
             limit: int | None = None) -> dict:
        return self.engine.sync_since(
            sheet_id, client_id, set(known_ids),
            limit or self.settings.sync_page_size,
        )

    # -------------------------------------------------------------- helpers
    def _next_seq(self, sheet_id: str, client_id: str) -> int:
        # server-issued ops continue after the highest seq seen for this client
        highest = -1
        for op in self.engine.store.all_ops():
            if op.sheet_id == sheet_id and op.client_id == client_id and op.seq > highest:
                highest = op.seq
        return highest + 1

    def _causal_ts(self, sheet_id: str, client_id: str) -> int:
        """Timestamp for a server-issued action (undo/redo).

        It must be later than every op already observed for this client so
        the action replays after them in canonical order; never below the
        wall clock, keeping it monotonic for the acting client.
        """
        highest = 0
        for op in self.engine.store.all_ops():
            if op.sheet_id == sheet_id and op.client_id == client_id and op.ts_ms > highest:
                highest = op.ts_ms
        return max(highest + 1, self.clock.now_ms())

    def _raw_to_op(self, sheet_id: str, req: RawOpRequest) -> Op:
        ts = req.ts_ms or self.clock.now_ms()
        target = req.target
        if req.kind == "PERMISSION" and req.scope == CELL_SCOPE and not target:
            target = cell_target(req.row, req.col)
        return Op(
            op_id=req.op_id or new_op_id(),
            sheet_id=sheet_id,
            client_id=req.client_id,
            seq=req.seq,
            ts_ms=ts,
            kind=req.kind,
            row=req.row, col=req.col, value=req.value,
            scope=req.scope, target=target, role=req.role, grantee=req.grantee,
        )

    def _dict_to_op(self, sheet_id: str, fallback_client: str, item: dict) -> Op:
        data = dict(item)
        data.setdefault("client_id", fallback_client)
        data.setdefault("sheet_id", sheet_id)
        data.setdefault("op_id", new_op_id())
        data.setdefault("seq", self._seq_for(data["client_id"], sheet_id, data))
        data.setdefault("ts_ms", self.clock.now_ms())
        if data.get("kind") == "PERMISSION" and data.get("scope") == CELL_SCOPE \
                and not data.get("target"):
            data["target"] = cell_target(int(data["row"]), int(data["col"]))
        return Op.from_dict(data)

    def _seq_for(self, client_id: str, sheet_id: str, data: dict) -> int:
        if "seq" in data:
            return int(data["seq"])
        return self._next_seq(sheet_id, client_id)


def build_service(data_dir: str, fsync: bool = True,
                  clock: Clock | None = None) -> SpreadsheetService:
    """Construct a service backed by a crash-recovered WAL store."""
    settings = get_settings(data_dir=data_dir, wal_fsync=fsync)
    store = Store.recover(settings.data_dir, fsync=settings.wal_fsync)
    engine = Engine(store, max_formula_depth=settings.max_formula_depth)
    return SpreadsheetService(engine, clock=clock, settings=settings)
