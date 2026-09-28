"""Operation model: the replicated log entry type and serialization.

Conflict resolution is last-writer-wins per cell with a globally deterministic
total order: (timestamp, client_id), so the final materialized state is a pure
function of the *set* of accepted operations, independent of arrival order.
"""

from __future__ import annotations

from dataclasses import dataclass

from spreadsheet.celladdr import CellAddr, parse_addr

OP_SET = "set"
OP_CLEAR = "clear"
OP_GRANT = "grant"
OP_REVOKE = "revoke"
OP_SHEET_READONLY = "sheet_readonly"

_KINDS = frozenset({OP_SET, OP_CLEAR, OP_GRANT, OP_REVOKE, OP_SHEET_READONLY})
_PERMISSION_KINDS = frozenset({OP_GRANT, OP_REVOKE, OP_SHEET_READONLY})


@dataclass(frozen=True, slots=True)
class OpId:
    client_id: str
    seq: int  # per-client monotonically increasing sequence number

    def format(self) -> str:
        return f"{self.client_id}:{self.seq}"


@dataclass(frozen=True, slots=True)
class Operation:
    id: OpId
    timestamp: int          # Lamport-ish monotonically ordered scalar
    sheet_id: str
    kind: str               # one of OP_* constants
    cell: CellAddr | None = None
    content: str | None = None
    target_client: str | None = None
    readonly: bool | None = None
    # Provenance: when this op is an undo/redo compensation.
    origin: OpId | None = None
    is_redo: bool = False
    compensation: bool = False
    # For an undo compensation: the user op whose value it restores (None=clear).
    restore: OpId | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown op kind: {self.kind!r}")
        if self.timestamp < 0:
            raise ValueError("timestamp must be non-negative")
        if self.id.seq < 0:
            raise ValueError("seq must be non-negative")

    def to_dict(self) -> dict:
        data: dict = {
            "client_id": self.id.client_id,
            "seq": self.id.seq,
            "ts": self.timestamp,
            "sheet": self.sheet_id,
            "kind": self.kind,
        }
        if self.cell is not None:
            data["cell"] = self.cell.format()
        if self.content is not None:
            data["content"] = self.content
        if self.target_client is not None:
            data["target"] = self.target_client
        if self.readonly is not None:
            data["readonly"] = self.readonly
        if self.origin is not None:
            data["origin"] = self.origin.format()
        if self.is_redo:
            data["redo"] = True
        if self.compensation:
            data["undo"] = True
        if self.restore is not None:
            data["restore"] = self.restore.format()
        return data

    @staticmethod
    def from_dict(data: dict) -> Operation:
        origin = None
        if data.get("origin"):
            cid, seq = data["origin"].rsplit(":", 1)
            origin = OpId(cid, int(seq))
        restore = None
        if data.get("restore"):
            cid, seq = data["restore"].rsplit(":", 1)
            restore = OpId(cid, int(seq))
        cell = parse_addr(data["cell"]) if data.get("cell") else None
        return Operation(
            id=OpId(data["client_id"], int(data["seq"])),
            timestamp=int(data["ts"]),
            sheet_id=data["sheet"],
            kind=data["kind"],
            cell=cell,
            content=data.get("content"),
            target_client=data.get("target"),
            readonly=data.get("readonly"),
            origin=origin,
            is_redo=bool(data.get("redo", False)),
            compensation=bool(data.get("undo", False)),
            restore=restore,
        )


def op_order_key(op: Operation) -> tuple[int, str]:
    """Deterministic total-order key: Lamport timestamp, then client id.

    seq is intentionally absent: a single client's ops are causally ordered
    and carry monotonically increasing timestamps, while cross-client ties
    must be broken by client id (never by sequence number).
    """
    return (op.timestamp, op.id.client_id)


def is_permission_kind(kind: str) -> bool:
    return kind in _PERMISSION_KINDS


def dedup_key(op: Operation) -> tuple[str, int]:
    return (op.id.client_id, op.id.seq)
