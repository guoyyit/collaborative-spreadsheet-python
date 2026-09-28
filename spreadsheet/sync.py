"""Causal incremental sync helpers (pure functions over the canonical log).

Each client tracks a version vector: for every originating client, the max
operation sequence it has seen. An operation is "new" to a client exactly when
its sequence is greater than the client's clock for that origin; this matches
the happened-before relation because each client's sequence numbers are
causally ordered. The canonical (timestamp, client_id, seq) order is itself a
linearization consistent with those causal relationships, so deltas can be
delivered in that order.

Properties:
* idempotent: re-requesting with the same vector returns nothing new;
* complete: repeated catch-up converges to exactly the full-sync result;
* non-blocking: a slow client never delays delivery to anyone else.
"""

from __future__ import annotations

from dataclasses import dataclass

from spreadsheet.operations import Operation, op_order_key


@dataclass(frozen=True, slots=True)
class VersionVector:
    """Per-originating-client max seen sequence number."""

    clocks: dict[str, int]

    @classmethod
    def empty(cls) -> VersionVector:
        return cls({})

    def merge(self, other: VersionVector) -> VersionVector:
        keys = self.clocks.keys() | other.clocks.keys()
        return VersionVector({k: max(self.clocks.get(k, 0), other.clocks.get(k, 0)) for k in keys})

    def covers(self, op: Operation) -> bool:
        return self.clocks.get(op.id.client_id, 0) >= op.id.seq

    def to_dict(self) -> dict[str, int]:
        return dict(self.clocks)

    @staticmethod
    def from_dict(data: dict[str, int] | None) -> VersionVector:
        return VersionVector(dict(data or {}))


def vector_from_ops(ops: list[Operation]) -> VersionVector:
    clocks: dict[str, int] = {}
    for op in ops:
        clocks[op.id.client_id] = max(clocks.get(op.id.client_id, 0), op.id.seq)
    return VersionVector(clocks)


def delta_for(ops: list[Operation], known: VersionVector) -> list[Operation]:
    """Return ops not covered by the known vector, in canonical causal order.

    The result equals the difference between a full sync (the whole ordered
    log) and the state the vector describes.
    """
    return sorted(
        (op for op in ops if not known.covers(op)),
        key=op_order_key,
    )
