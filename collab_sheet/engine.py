"""Deterministic merge engine.

Confluence rule (cells)
-----------------------
For each cell, consider all *live* SET_CELL ops (ops not undone by their own
author).  The winner is the greatest by::

    (ts_ms, client_id, seq, op_id)

This key uses only op-intrinsic fields, so the same set of ops produces the
same final state regardless of arrival order; every cell exposes the winning
op as provenance.  An author's later op naturally supersedes their earlier
one; an offline replay never overwrites another client's committed write
unless it actually wins this deterministic comparison.

Undo / redo
-----------
UNDO/REDO ops only toggle whether *the acting client's own* target op is
live, so undoing an op that participated in a concurrent merge never rolls
back anyone else's writes.

Formulas
--------
After applying the winning SET_CELL op for every coordinate, formula cells
are evaluated in dependency (topological) order.  A SET_CELL formula op that
would introduce a cycle into the *current* graph is rejected at commit time
with ``#CYCLE!`` diagnostics; defense-in-depth #CYCLE! evaluation remains for
races.  Error cells propagate as errors, never as numbers.

Synchronization
---------------
State is replayed from the durable log in *canonical* (op-key sorted) order.
Clients report the set of op ids they have already observed and receive
exactly the missing ops in canonical order - equivalent to a full sync,
independent of how long they were disconnected.
"""

import threading
from collections.abc import Iterable

from . import formula
from .models import (
    CELL_BLANK,
    CELL_ERROR,
    CELL_NUMBER,
    CELL_TEXT,
    REDO,
    SET_CELL,
    UNDO,
    CellView,
    CommitResult,
    Op,
    Rejection,
    TableMeta,
    TableSnapshot,
)
from .permissions import (
    TABLE_SCOPE,
    PermissionView,
    build_permission_record,
    cell_target,
    is_permission_op,
    role_rank,
)
from .store import Store


def _op_sort_key(op: Op) -> tuple:
    """Canonical, arrival-independent ordering key."""
    return (op.ts_ms, op.client_id, op.seq, op.op_id)


class Engine:
    def __init__(self, store: Store, max_formula_depth: int = 64) -> None:
        self.store = store
        self.max_formula_depth = max_formula_depth
        self._lock = threading.RLock()
        self._derived: dict[str, _Derived] = {}
        self.rebuild()

    # ============================================================ lifecycle
    def rebuild(self) -> None:
        """Recompute all derived state from the durable log."""
        with self._lock:
            self._derived = {}
            for meta in self.store.list_tables():
                self._derived[meta.sheet_id] = _Derived(meta.sheet_id)
            ordered = sorted(self.store.all_ops(), key=_op_sort_key)
            for op in ordered:
                der = self._derived.get(op.sheet_id)
                if der is None:
                    continue
                der.canonical_all.append(op)
                der.record_op(op)
            for der in self._derived.values():
                der.compute()

    def create_table(self, sheet_id: str, owner_id: str, now_ms: int) -> TableMeta:
        with self._lock:
            existing = self.store.get_table_meta(sheet_id)
            if existing is not None:
                return existing
            meta = TableMeta(sheet_id=sheet_id, owner_id=owner_id, created_ms=now_ms)
            self.store.create_table(meta)
            self._derived[sheet_id] = _Derived(sheet_id)
            # owner bootstrap permission (durable like everything else)
            grant = Op(
                op_id=f"perm-owner-{sheet_id}",
                sheet_id=sheet_id,
                client_id=owner_id,
                grantee=owner_id,
                seq=0,
                ts_ms=now_ms,
                kind="PERMISSION",
                scope=TABLE_SCOPE,
                target="*",
                role="owner",
            )
            self.store.append_ops([grant])
            self._derived[sheet_id].record_op(grant)
            self._derived[sheet_id].compute()
            return meta

    # =============================================================== commit
    def commit(self, ops: Iterable[Op]) -> CommitResult:
        """Validate a batch, persist the accepted ops in one atomic frame,
        then recompute.  Duplicate ids are acked (idempotent)."""
        with self._lock:
            incoming = list(ops)
            result = CommitResult()
            if not incoming:
                result.version = self.store.version()
                return result

            # 1) structural / semantic validation against current state;
            #    idempotent duplicates are accepted acks and need no checks.
            known = {o.op_id for o in self.store.all_ops()}
            batch_ids = {o.op_id for o in incoming}
            if len(batch_ids) != len(incoming):
                return self._reject_all(incoming, "bad_batch", "duplicate op_id inside batch")
            fresh: list[Op] = []
            for op in incoming:
                if op.op_id in known:
                    result.accepted.append(op.op_id)
                    continue
                err = self._validate(op)
                if err is not None:
                    result.rejected.append(Rejection(op.op_id, err[0], err[1]))
                else:
                    fresh.append(op)

            # 2) atomic durability: batch semantics - if anything structurally
            #    wrong, none of the *new* ops become visible.  Authorization
            #    failures likewise reject the whole batch atomically.
            if result.rejected:
                result.version = self.store.version()
                return result

            committed, version = self.store.append_ops(fresh)
            result.accepted.extend(o.op_id for o in committed)
            result.version = version

            # 3) derive from the full canonical log (deterministic); only the
            #    affected sheet needs a projection refresh.
            if committed:
                affected = {o.sheet_id for o in committed}
                self._rebuild_sheets(affected)
            return result

    def _rebuild_sheets(self, sheet_ids: set[str]) -> None:
        """Recompute derived state for the given sheets from the durable log."""
        per_sheet: dict[str, list[Op]] = {sid: [] for sid in sheet_ids}
        for op in self.store.all_ops():
            if op.sheet_id in per_sheet:
                per_sheet[op.sheet_id].append(op)
        for sid, ops in per_sheet.items():
            der = _Derived(sid)
            for op in sorted(ops, key=_op_sort_key):
                der.canonical_all.append(op)
                der.record_op(op)
            der.compute()
            self._derived[sid] = der

    def _reject_all(self, ops: list[Op], reason: str, detail: str) -> CommitResult:
        return CommitResult(
            accepted=[],
            rejected=[Rejection(o.op_id, reason, detail) for o in ops],
            version=self.store.version(),
        )

    # ---------------------------------------------------------------- checks
    def _validate(self, op: Op) -> tuple[str, str] | None:
        der = self._derived.get(op.sheet_id)
        if der is None:
            return ("no_such_sheet", f"unknown sheet_id {op.sheet_id!r}")
        if op.client_id == "":
            return ("bad_op", "client_id is required")
        if op.seq < 0:
            return ("bad_op", "seq must be >= 0")

        if op.kind == SET_CELL:
            if op.row < 0 or op.col < 0:
                return ("bad_op", "row/col must be >= 0")
            if not der.perms.can_edit(op.sheet_id, op.client_id, op.row, op.col):
                return ("forbidden", (
                    f"client {op.client_id!r} may not edit "
                    f"{cell_target(op.row, op.col)}"))
            if formula.is_formula(op.value):
                try:
                    parsed = formula.parse_formula(op.value)
                except formula.FormulaError as exc:
                    return ("bad_formula", f"{exc.code}: {exc.message}")
                path = formula.detect_cycle(
                    (op.row, op.col), parsed.refs,
                    der.proposed_graph((op.row, op.col), parsed.refs),
                    max_depth=self.max_formula_depth,
                )
                if path is not None:
                    chain = " -> ".join(formula.encode_ref(r, c) for r, c in path)
                    return ("cycle", f"#CYCLE!: {chain}")
            return None

        if op.kind == "PERMISSION":
            return self._validate_permission(op, der)

        if op.kind in (UNDO, REDO):
            target_id = op.undone_id or op.redone_id
            if not target_id:
                return ("bad_op", "undo/redo requires a target op id")
            target = der.set_ops.get(target_id)
            if target is None:
                return ("unknown_target", f"op {target_id!r} not found in this sheet")
            if target.client_id != op.client_id:
                return ("forbidden", "can only undo/redo your own operations")
            return None

        return ("bad_op", f"unknown op kind {op.kind!r}")

    def _validate_permission(self, op: Op, der: "_Derived") -> tuple[str, str] | None:
        if op.scope not in (TABLE_SCOPE, "cell"):
            return ("bad_op", f"invalid scope {op.scope!r}")
        if op.role not in ("", "reader", "writer", "admin", "owner"):
            return ("bad_op", f"invalid role {op.role!r}")
        if op.scope == "cell":
            try:
                r, c = (int(x) for x in op.target.split(","))
            except (ValueError, AttributeError):
                return ("bad_op", "cell grant target must be 'row,col'")
            if r < 0 or c < 0:
                return ("bad_op", "cell coordinates must be >= 0")
        # Only owner can grant owner/admin; admin can grant writer/reader.
        granter_rank = role_rank(der.perms.table_role(op.sheet_id, op.client_id))
        granted_rank = role_rank(op.role)
        if op.role in ("owner", "admin"):
            if not der.perms.is_owner(op.sheet_id, op.client_id):
                return ("forbidden", "only owners can grant owner/admin roles")
        else:
            if granter_rank < role_rank("admin"):
                return ("forbidden", "admin or owner role required to change permissions")
        # cannot raise someone above your own effective rank
        if op.role and granted_rank > granter_rank:
            return ("forbidden", "cannot grant a role above your own")
        return None

    # ================================================================ reads
    def snapshot(self, sheet_id: str) -> TableSnapshot | None:
        with self._lock:
            der = self._derived.get(sheet_id)
            if der is None:
                return None
            return TableSnapshot(sheet_id=sheet_id, version=self.store.version(), cells=dict(der.cells))

    def cell(self, sheet_id: str, row: int, col: int) -> CellView | None:
        with self._lock:
            der = self._derived.get(sheet_id)
            if der is None:
                return None
            return der.cells.get((row, col))

    def sync_since(self, sheet_id: str, client_id: str, known_ids: set[str], limit: int = 1000) -> dict:
        """Return canonical ops the client has not seen yet.

        The client's high-water mark is the *set* of op ids it already has,
        so a slow / offline client never blocks anyone and reconnecting
        converges to exactly the full-state result.
        """
        with self._lock:
            der = self._derived.get(sheet_id)
            if der is None:
                return {"sheet_id": sheet_id, "ops": [], "version": self.store.version(),
                        "next_known": sorted(known_ids), "has_more": False}
            missing = [op for op in der.canonical_all if op.op_id not in known_ids]
            page = missing[:limit]
            next_known = known_ids | {op.op_id for op in page}
            return {
                "sheet_id": sheet_id,
                "ops": [op.to_dict() for op in page],
                "version": self.store.version(),
                "next_known": sorted(next_known),
                "has_more": len(missing) > len(page),
            }

    def undo_targets(self, sheet_id: str, client_id: str) -> list[str]:
        with self._lock:
            return list(self._derived[sheet_id].undo_stack.get(client_id, []))

    def redo_targets(self, sheet_id: str, client_id: str) -> list[str]:
        with self._lock:
            return list(self._derived[sheet_id].redo_stack.get(client_id, []))


# ===========================================================================
# per-sheet derived state
# ===========================================================================
class _Derived:
    def __init__(self, sheet_id: str) -> None:
        self.sheet_id = sheet_id
        self.perm_records: list = []
        self.perms = PermissionView()
        self.set_ops: dict[str, Op] = {}          # op_id -> SET_CELL op
        self.undone: dict[str, set[str]] = {}     # client -> {set op ids currently undone}
        self.toggle_log: dict[str, list[tuple[str, bool]]] = {}  # client -> [(target_id, live?)] in canonical order
        self.undo_stack: dict[str, list[str]] = {}
        self.redo_stack: dict[str, list[str]] = {}
        self.canonical_all: list[Op] = []         # every op, canonical order
        self.graph: dict[tuple[int, int], frozenset[tuple[int, int]]] = {}
        self.cells: dict[tuple[int, int], CellView] = {}

    # ------------------------------------------------------------- ingestion
    def record_op(self, op: Op) -> None:
        """Fold one canonical-ordered op into the raw projections."""
        if op.kind == SET_CELL:
            self.set_ops[op.op_id] = op
        elif is_permission_op(op):
            rec = build_permission_record(op)
            self.perm_records.append(rec)
            self.perms.apply(rec)
        elif op.kind in (UNDO, REDO):
            target_id = op.undone_id or op.redone_id
            self.toggle_log.setdefault(op.client_id, []).append(
                (target_id, op.kind == REDO)
            )

    def proposed_graph(
        self, target: tuple[int, int], refs: frozenset[tuple[int, int]]
    ) -> dict[tuple[int, int], frozenset[tuple[int, int]]]:
        """Graph as it would look after the candidate formula is installed."""
        g = dict(self.graph)
        g[target] = refs
        return g

    # ------------------------------------------------------------ projection
    def compute(self) -> None:
        """Recompute winners, formula graph and evaluated cells from scratch."""
        # --- derive undo/redo liveness and per-client stacks --------------
        # Replay each client's timeline of own SET_CELL ops and UNDO/REDO
        # toggles in canonical order: a fresh edit clears redo history
        # (standard linear undo/redo); undo only affects the actor's own op.
        events: dict[str, list[tuple]] = {}
        for op in self.canonical_all:
            if op.kind == SET_CELL:
                events.setdefault(op.client_id, []).append(("set", op.op_id))
            elif op.kind in (UNDO, REDO):
                target_id = op.undone_id or op.redone_id
                events.setdefault(op.client_id, []).append(
                    ("undo" if op.kind == UNDO else "redo", target_id)
                )
        for client, timeline in events.items():
            undone: set[str] = set()
            undo_stack: list[str] = []
            redo_stack: list[str] = []
            for kind, target_id in timeline:
                if kind == "set":
                    # a fresh edit after undos collapses redo history
                    redo_stack.clear()
                    if target_id in undo_stack:
                        undo_stack.remove(target_id)
                    undo_stack.append(target_id)
                    undone.discard(target_id)
                elif kind == "undo":
                    if target_id in undo_stack:
                        undo_stack.remove(target_id)
                    redo_stack = [t for t in redo_stack if t != target_id]
                    redo_stack.append(target_id)
                    undone.add(target_id)
                else:  # redo
                    if target_id in redo_stack:
                        redo_stack.remove(target_id)
                    undo_stack = [t for t in undo_stack if t != target_id]
                    undo_stack.append(target_id)
                    undone.discard(target_id)
            self.undone[client] = undone
            self.undo_stack[client] = undo_stack
            self.redo_stack[client] = redo_stack

        # live SET_CELL ops in canonical order (last write per coordinate wins)
        set_canonical = sorted(self.set_ops.values(), key=_op_sort_key)
        winners: dict[tuple[int, int], Op] = {}
        for op in set_canonical:
            if op.op_id in self.undone.get(op.client_id, set()):
                continue
            winners[(op.row, op.col)] = op

        # raw views for non-formula cells; collect formula graph
        self.graph = {}
        self.cells = {}
        formula_ops: dict[tuple[int, int], Op] = {}
        for (r, c), op in winners.items():
            if formula.is_formula(op.value):
                formula_ops[(r, c)] = op
                try:
                    parsed = formula.parse_formula(op.value)
                    self.graph[(r, c)] = parsed.refs
                except formula.FormulaError:
                    self.graph[(r, c)] = frozenset()
            else:
                kind, value = formula.classify(op.value)
                if kind == CELL_BLANK:
                    # a live write of empty string clears the cell
                    self.cells[(r, c)] = CellView(
                        r, c, "", CELL_BLANK, self._provenance(op)
                    )
                else:
                    display = formula.format_number(value) if kind == CELL_NUMBER else str(value)
                    self.cells[(r, c)] = CellView(
                        r, c, display, kind, self._provenance(op)
                    )

        self._evaluate_formulas(winners, formula_ops)

    def _evaluate_formulas(
        self, winners: dict[tuple[int, int], Op], formula_ops: dict[tuple[int, int], Op]
    ) -> None:
        # Kahn-style evaluation: repeatedly evaluate formulas whose deps are done.
        remaining = dict(formula_ops)
        progress = True
        cyclic: set[tuple[int, int]] = set()
        while remaining and progress:
            progress = False
            for coord in list(remaining):
                refs = self.graph.get(coord, frozenset())
                if any(ref in remaining for ref in refs):
                    continue
                op = remaining.pop(coord)
                self._eval_one(coord, op, winners, cyclic_guard=cyclic)
                progress = True
        # anything left participates in a cycle (or depends on one)
        for coord, op in remaining.items():
            self.cells[coord] = CellView(
                coord[0], coord[1], formula.CycleError.code, CELL_ERROR,
                {**self._provenance(op), "error": formula.CycleError.code,
                 "detail": "formula participates in a reference cycle"},
                error=formula.CycleError.code,
            )
            cyclic.add(coord)

    def _eval_one(
        self,
        coord: tuple[int, int],
        op: Op,
        winners: dict[tuple[int, int], Op],
        cyclic_guard: set[tuple[int, int]],
    ) -> None:
        r, c = coord
        try:
            parsed = formula.parse_formula(op.value)
        except formula.FormulaError as exc:
            self._set_error_cell(r, c, op, exc)
            return

        values: dict[tuple[int, int], object] = {}
        for ref in parsed.refs:
            values[ref] = self._resolved_input(ref, winners)

        try:
            number = formula.evaluate(op.value, values)
        except formula.FormulaError as exc:
            self._set_error_cell(r, c, op, exc)
            return
        self.cells[coord] = CellView(
            r, c, formula.format_number(number), CELL_NUMBER,
            {**self._provenance(op), "computed_from": op.value},
        )

    def _resolved_input(self, ref: tuple[int, int], winners: dict[tuple[int, int], Op]) -> object:
        """Numeric/text/error value of a dependency, without implicit coercion
        of missing references except empty->0 (handled in the evaluator)."""
        view = self.cells.get(ref)
        if view is None:
            # no live write at all: treat as empty (0 in arithmetic)
            return None  # evaluator maps missing key to 0
        if view.kind == CELL_ERROR:
            return formula.error_by_code(view.error or "#ERROR!")
        if view.kind == CELL_NUMBER:
            return float(view.value)
        if view.kind == CELL_TEXT:
            return view.value
        if view.kind == CELL_BLANK:
            return None
        return view.value

    def _set_error_cell(self, r: int, c: int, op: Op, exc: formula.FormulaError) -> None:
        self.cells[(r, c)] = CellView(
            r, c, exc.code, CELL_ERROR,
            {**self._provenance(op), "error": exc.code, "detail": exc.message},
            error=exc.code,
        )

    @staticmethod
    def _provenance(op: Op) -> dict:
        return {
            "op_id": op.op_id,
            "client_id": op.client_id,
            "seq": op.seq,
            "ts_ms": op.ts_ms,
        }
