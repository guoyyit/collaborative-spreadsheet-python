"""Table- and cell-level read/edit permissions.

Permission policy
-----------------
Every table starts with its creator holding the ``owner`` role (table scope).

Roles, most powerful first::

    owner  - everything, including permission administration
    admin  - edit anywhere + grant/revoke writer/reader roles
    writer - edit cells + edit formulas
    reader - read-only

Permission *changes* are themselves PERMISSION ops in the causal log and are
merged with the same deterministic last-writer-wins rule as cells, keyed by
``(sheet_id, scope, target, client_id)``.  Therefore permission changes and
concurrent edits have one fixed effective ordering: an edit is checked
against the permission record with the greatest ``(ts_ms, client_id, seq,
op_id)``; ties and replays are therefore stable.

Cell-scoped grants override the table-scoped role for the exact cell.
A grant with ``role == ""`` revokes any record for that key.
"""

from dataclasses import dataclass

from .models import PERMISSION, Op

READER = "reader"
WRITER = "writer"
ADMIN = "admin"
OWNER = "owner"

#: rank used for comparisons; None means "no role"
ROLE_RANK = {READER: 1, WRITER: 2, ADMIN: 3, OWNER: 4}
VALID_ROLES = ("", READER, WRITER, ADMIN, OWNER)

TABLE_SCOPE = "table"
CELL_SCOPE = "cell"


def role_rank(role: str | None) -> int:
    return ROLE_RANK.get(role or "", 0)


class PermissionError_(Exception):
    """An operation is not authorized under the current effective policy."""

    def __init__(self, message: str, op_id: str = "") -> None:
        super().__init__(message)
        self.op_id = op_id
        self.message = message


@dataclass(frozen=True)
class PermissionRecord:
    sheet_id: str
    scope: str
    target: str          # "*" for table scope, else "row,col"
    client_id: str       # grantee: the user whose access this record describes
    role: str
    ts_ms: int
    seq: int
    op_id: str

    def sort_key(self) -> tuple:
        return (self.ts_ms, self.client_id, self.seq, self.op_id)


def is_permission_op(op: Op) -> bool:
    return op.kind == PERMISSION


def cell_target(row: int, col: int) -> str:
    return f"{row},{col}"


def build_permission_record(op: Op) -> PermissionRecord:
    return PermissionRecord(
        sheet_id=op.sheet_id,
        scope=op.scope,
        target=op.target,
        client_id=op.grantee or op.client_id,
        role=op.role,
        ts_ms=op.ts_ms,
        seq=op.seq,
        op_id=op.op_id,
    )


class PermissionView:
    """Effective permissions derived from LWW permission records."""

    def __init__(self) -> None:
        # sheet -> client -> table role record
        self._table: dict[str, dict[str, PermissionRecord]] = {}
        # sheet -> "r,c" -> client -> cell role record
        self._cells: dict[str, dict[str, dict[str, PermissionRecord]]] = {}

    @classmethod
    def from_records(cls, records: list[PermissionRecord]) -> "PermissionView":
        view = cls()
        for rec in sorted(records, key=lambda r: r.sort_key()):
            view.apply(rec)
        return view

    def apply(self, rec: PermissionRecord) -> None:
        """Apply one record if it is newer than the current one for its key."""
        bucket: dict
        if rec.scope == TABLE_SCOPE:
            bucket = self._table.setdefault(rec.sheet_id, {})
            key = rec.client_id
        elif rec.scope == CELL_SCOPE:
            bucket = self._cells.setdefault(rec.sheet_id, {}).setdefault(rec.target, {})
            key = rec.client_id
        else:
            return
        current = bucket.get(key)
        if current is None or rec.sort_key() > current.sort_key():
            if rec.role:
                bucket[key] = rec
            else:
                bucket.pop(key, None)

    # ---------------------------------------------------------------- queries
    def table_role(self, sheet_id: str, client_id: str) -> str | None:
        rec = self._table.get(sheet_id, {}).get(client_id)
        return rec.role if rec else None

    def role_for(self, sheet_id: str, client_id: str, row: int, col: int) -> str | None:
        cell_rec = self._cells.get(sheet_id, {}).get(cell_target(row, col), {}).get(client_id)
        if cell_rec is not None:
            return cell_rec.role
        return self.table_role(sheet_id, client_id)

    def can_read(self, sheet_id: str, client_id: str) -> bool:
        return self.table_role(sheet_id, client_id) is not None

    def can_edit(self, sheet_id: str, client_id: str, row: int = -1, col: int = -1) -> bool:
        if row < 0 or col < 0:
            return role_rank(self.table_role(sheet_id, client_id)) >= role_rank(WRITER)
        return role_rank(self.role_for(sheet_id, client_id, row, col)) >= role_rank(WRITER)

    def can_admin(self, sheet_id: str, client_id: str) -> bool:
        return role_rank(self.table_role(sheet_id, client_id)) >= role_rank(ADMIN)

    def is_owner(self, sheet_id: str, client_id: str) -> bool:
        return self.table_role(sheet_id, client_id) == OWNER
