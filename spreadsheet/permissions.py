"""Permission model and deterministic evaluation against the op log.

- Sheet owner (creator) always has write access.
- Sheet may be switched read-only for non-owners (OP_SHEET_READONLY).
- Per-cell grants (OP_GRANT / OP_REVOKE) allow/revoke a client on one cell.
A write op is permitted iff, considering only permission ops ordered before it
in the deterministic total order, the client is not blocked by sheet read-only
and is either the owner or has an active cell grant. Permission-changing ops
themselves require owner rights.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from spreadsheet.celladdr import CellAddr
from spreadsheet.operations import (
    OP_GRANT,
    OP_REVOKE,
    OP_SHEET_READONLY,
    Operation,
    is_permission_kind,
)


@dataclass(slots=True)
class PermissionState:
    owner: str
    sheet_readonly: bool = False
    grants: set[tuple[str, CellAddr]] = field(default_factory=set)

    def can_write(self, client_id: str, cell: CellAddr | None) -> bool:
        if client_id == self.owner:
            return True
        if self.sheet_readonly:
            return False
        if cell is None:
            return False
        return (client_id, cell) in self.grants


def apply_permission_op(state: PermissionState, op: Operation) -> None:
    """Mutate state according to a permission op in total-order."""
    if op.kind == OP_SHEET_READONLY:
        state.sheet_readonly = bool(op.readonly)
    elif op.kind == OP_GRANT and op.cell is not None and op.target_client is not None:
        state.grants.add((op.target_client, op.cell))
    elif op.kind == OP_REVOKE and op.cell is not None and op.target_client is not None:
        state.grants.discard((op.target_client, op.cell))


def is_permission_op(op: Operation) -> bool:
    return is_permission_kind(op.kind)
