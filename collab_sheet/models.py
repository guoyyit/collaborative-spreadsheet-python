"""Core data models.

An :class:`Op` is the unit of causal history.  Every op is created by a client
and carries:

* ``op_id``    - globally unique id (deduplication / idempotency key);
* ``client_id``/``seq`` - per-client monotonically increasing sequence;
* ``ts_ms``    - client wall clock at creation time, used as the deterministic
  last-writer-wins ordering key (ties broken by ``client_id`` then ``seq``).

Because merge only depends on the *set* of ops and fields inside them, the
same set of operations converges to the same state no matter what order the
server receives them in.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

# -- operation kinds --------------------------------------------------------
CREATE_TABLE = "CREATE_TABLE"
SET_CELL = "SET_CELL"
PERMISSION = "PERMISSION"
UNDO = "UNDO"
REDO = "REDO"

#: cell content classifications
CELL_TEXT = "text"
CELL_NUMBER = "number"
CELL_FORMULA = "formula"
CELL_ERROR = "error"
CELL_BLANK = "blank"


@dataclass(frozen=True)
class CellRef:
    sheet_id: str
    row: int
    col: int


@dataclass(frozen=True)
class Op:
    op_id: str
    sheet_id: str
    client_id: str
    seq: int
    ts_ms: int
    kind: str
    # SET_CELL target / payload
    row: int = -1
    col: int = -1
    value: str = ""
    # PERMISSION payload: client_id is the granter (actor), grantee is target user
    scope: str = ""        # "table" | "cell"
    target: str = ""       # "*" or "row,col"
    role: str = ""         # reader | writer | admin | owner | "" (revoke)
    grantee: str = ""
    # UNDO / REDO payload
    undone_id: str = ""
    redone_id: str = ""
    toggled: bool = False  # resolved live/dead state for UNDO(false)/REDO(true)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Op":
        known = {f for f in Op.__dataclass_fields__}  # type: ignore[attr-defined]
        return Op(**{k: v for k, v in d.items() if k in known})


@dataclass
class TableMeta:
    sheet_id: str
    owner_id: str
    created_ms: int = 0


@dataclass
class Rejection:
    op_id: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommitResult:
    accepted: list[str] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": list(self.accepted),
            "rejected": [r.to_dict() for r in self.rejected],
            "version": self.version,
        }


@dataclass
class CellView:
    row: int
    col: int
    value: str
    kind: str
    provenance: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TableSnapshot:
    sheet_id: str
    version: int
    cells: dict[tuple[int, int], CellView]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_id": self.sheet_id,
            "version": self.version,
            "cells": [c.to_dict() for c in sorted(self.cells.values(), key=lambda c: (c.row, c.col))],
        }
