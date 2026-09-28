"""Deterministic merge engine.

Maintains the canonical (total-order sorted, deduplicated) log of accepted
operations and the materialized grid. The materialized state is a pure
function of the set of accepted ops: ops are sorted by
(timestamp, client_id, seq) and re-evaluated topologically.

Permission evaluation reuses the same total order, so whether a write is
accepted depends only on the set of submitted ops, never on arrival order:
permission ops take effect at their position in the total order relative to
the writes they guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from spreadsheet.celladdr import CellAddr
from spreadsheet.errors import (
    CIRCULAR_ERROR,
    CellError,
    FormulaSyntaxError,
)
from spreadsheet.formula import (
    ParsedFormula,
    Value,
    eval_formula,
    literal_value,
    parse_formula,
)
from spreadsheet.operations import (
    OP_CLEAR,
    OP_SET,
    Operation,
    op_order_key,
)
from spreadsheet.permissions import (
    PermissionState,
    apply_permission_op,
    is_permission_op,
)


@dataclass(slots=True)
class SheetState:
    sheet_id: str
    owner: str
    ops: list[Operation] = field(default_factory=list)
    raw: dict[CellAddr, str] = field(default_factory=dict)
    values: dict[CellAddr, Value] = field(default_factory=dict)
    errors: dict[CellAddr, CellError] = field(default_factory=dict)
    parsed: dict[CellAddr, ParsedFormula] = field(default_factory=dict)
    permissions: PermissionState | None = None


class MergeEngine:
    def __init__(self, sheet_id: str, owner: str) -> None:
        self._state = SheetState(
            sheet_id=sheet_id,
            owner=owner,
            permissions=PermissionState(owner=owner),
        )

    @property
    def state(self) -> SheetState:
        return self._state

    # ------------------------------------------------------------------ #
    # Log management
    # ------------------------------------------------------------------ #
    def has_op(self, client_id: str, seq: int) -> bool:
        return any(o.id.client_id == client_id and o.id.seq == seq for o in self._state.ops)

    def add_ops(self, ops: list[Operation]) -> tuple[list[Operation], list[tuple[Operation, str]]]:
        """Merge candidate ops into the canonical log.

        Candidates are combined with the existing accepted log, deduplicated
        by (client_id, seq), sorted in total order, and replayed through the
        permission rules. Returns (newly accepted ops, rejected (op, reason)).
        Rejected ops never enter the log.
        """
        by_id: dict[tuple[str, int], Operation] = {
            (o.id.client_id, o.id.seq): o for o in self._state.ops
        }
        rejected: list[tuple[Operation, str]] = []
        for op in ops:
            key = (op.id.client_id, op.id.seq)
            if key in by_id:
                continue  # duplicate delivery -> idempotent, silently ignored
            by_id[key] = op
        ordered = sorted(by_id.values(), key=op_order_key)

        perms = PermissionState(owner=self._state.owner)
        accepted: list[Operation] = []
        old_ids = {(o.id.client_id, o.id.seq) for o in self._state.ops}
        newly: list[Operation] = []
        for op in ordered:
            reason = self._check_permission(perms, op)
            if reason is not None:
                rejected.append((op, reason))
                continue
            if is_permission_op(op):
                apply_permission_op(perms, op)
            accepted.append(op)
            if (op.id.client_id, op.id.seq) not in old_ids:
                newly.append(op)

        self._state.ops = accepted
        self._state.permissions = perms
        self._materialize()
        return newly, rejected

    @staticmethod
    def _check_permission(perms: PermissionState, op: Operation) -> str | None:
        if is_permission_op(op):
            if op.id.client_id != perms.owner:
                return "only the sheet owner may change permissions"
            return None
        if not perms.can_write(op.id.client_id, op.cell):
            return f"client {op.id.client_id!r} may not write {op.cell.format() if op.cell else '<sheet>'}"
        return None

    # ------------------------------------------------------------------ #
    # Materialization
    # ------------------------------------------------------------------ #
    def rebuild(self) -> None:
        """Re-derive raw/values/permissions from the canonical op log."""
        perms = PermissionState(owner=self._state.owner)
        raw: dict[CellAddr, str] = {}
        for op in sorted(self._state.ops, key=op_order_key):
            if is_permission_op(op):
                apply_permission_op(perms, op)
            elif op.kind == OP_SET and op.cell is not None:
                if op.content:
                    raw[op.cell] = op.content
                else:
                    raw.pop(op.cell, None)  # empty SET == clear
            elif op.kind == OP_CLEAR and op.cell is not None:
                raw.pop(op.cell, None)
        self._state.permissions = perms
        self._state.raw = raw
        self._recompute(raw)

    def _materialize(self) -> None:
        raw: dict[CellAddr, str] = {}
        for op in self._state.ops:  # already total-order sorted
            if op.kind == OP_SET and op.cell is not None:
                if op.content:
                    raw[op.cell] = op.content
                else:
                    raw.pop(op.cell, None)  # empty SET == clear
            elif op.kind == OP_CLEAR and op.cell is not None:
                raw.pop(op.cell, None)
        self._state.raw = raw
        self._recompute(raw)

    def _recompute(self, raw: dict[CellAddr, str]) -> None:
        parsed: dict[CellAddr, ParsedFormula] = {}
        syntax_errors: dict[CellAddr, CellError] = {}
        for cell, content in raw.items():
            if content.startswith("="):
                try:
                    parsed[cell] = parse_formula(content[1:])
                except FormulaSyntaxError as exc:
                    syntax_errors[cell] = CellError("SYNTAX", str(exc))

        cyclic = _find_cyclic_cells(parsed)
        values: dict[CellAddr, Value] = {}
        errors: dict[CellAddr, CellError] = dict(syntax_errors)

        for cell in cyclic:
            errors[cell] = CellError(
                CIRCULAR_ERROR,
                f"circular reference involving {cell.format()}",
            )

        def get(addr: CellAddr) -> Value | None:
            if addr in errors:
                return errors[addr]
            if addr in values:
                return values[addr]
            content = raw.get(addr)
            if content is None:
                return None
            if content.startswith("="):
                # A formula reached without an evaluated value must be part of
                # a cycle (handled) or a yet-unordered node (should not happen
                # thanks to topological evaluation).
                return CellError(CIRCULAR_ERROR, "unordered formula")
            return literal_value(content)

        for cell in _topo_order(parsed, cyclic):
            values[cell] = eval_formula(parsed[cell], get)

        # Plain literal cells are still addressable with their numeric value.
        for cell, content in raw.items():
            if cell in parsed or cell in errors:
                continue
            values[cell] = literal_value(content)

        self._state.parsed = parsed
        self._state.values = values
        self._state.errors = errors

    def get_value(self, cell: CellAddr) -> Value:
        if cell in self._state.errors:
            return self._state.errors[cell]
        return self._state.values.get(cell, 0)

    def get_raw(self, cell: CellAddr) -> str | None:
        return self._state.raw.get(cell)

    def can_write(self, client_id: str, cell: CellAddr | None) -> bool:
        assert self._state.permissions is not None
        return self._state.permissions.can_write(client_id, cell)


# --------------------------------------------------------------------------- #
# Dependency-graph algorithms
# --------------------------------------------------------------------------- #
def _find_cyclic_cells(parsed: dict[CellAddr, ParsedFormula]) -> set[CellAddr]:
    """Tarjan SCC; mark cells in nontrivial SCCs (and self-loops) cyclic."""
    index_counter = 0
    stack: list[CellAddr] = []
    on_stack: set[CellAddr] = set()
    indices: dict[CellAddr, int] = {}
    lowlink: dict[CellAddr, int] = {}
    cyclic: set[CellAddr] = set()

    def strongconnect(v: CellAddr) -> None:
        nonlocal index_counter
        indices[v] = index_counter
        lowlink[v] = index_counter
        index_counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in parsed[v].dependencies:
            if w not in parsed:
                continue
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: list[CellAddr] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                cyclic.update(comp)
            elif v in parsed[v].dependencies:
                cyclic.add(v)

    for node in parsed:
        if node not in indices:
            strongconnect(node)
    return cyclic


def _topo_order(
    parsed: dict[CellAddr, ParsedFormula], cyclic: set[CellAddr]
) -> list[CellAddr]:
    """Kahn order restricted to non-cyclic formula cells.

    Formula dependencies within formula cells establish edges; literal/empty
    dependencies are resolved on demand and impose no ordering constraint.
    """
    nodes = [c for c in parsed if c not in cyclic]
    node_set = set(nodes)
    indeg: dict[CellAddr, int] = {c: 0 for c in nodes}
    dependents: dict[CellAddr, list[CellAddr]] = {c: [] for c in nodes}
    for c in nodes:
        for dep in parsed[c].dependencies:
            if dep in node_set:
                indeg[c] += 1
                dependents[dep].append(c)
    # Stable, deterministic queue.
    ready = sorted((c for c in nodes if indeg[c] == 0), key=lambda a: (a.row, a.col))
    order: list[CellAddr] = []
    while ready:
        cell = ready.pop(0)
        order.append(cell)
        new_ready = []
        for dep in dependents[cell]:
            indeg[dep] -= 1
            if indeg[dep] == 0:
                new_ready.append(dep)
        if new_ready:
            ready.extend(sorted(new_ready, key=lambda a: (a.row, a.col)))
            ready.sort(key=lambda a: (a.row, a.col))
    return order
