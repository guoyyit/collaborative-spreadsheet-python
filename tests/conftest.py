"""Shared fixtures/helpers for the collaborative spreadsheet test suite.

Tests model clients that maintain a Lamport clock: each generated op gets a
strictly larger timestamp, and a client that has observed state at time ``t``
bumps its clock past ``t`` before editing again.
"""

from __future__ import annotations

import pytest

from spreadsheet.celladdr import parse_addr
from spreadsheet.operations import Operation, OpId
from spreadsheet.service import SpreadsheetService
from spreadsheet.storage import WALStorage


class Clock:
    """Simple per-client Lamport clock."""

    def __init__(self, start: int = 0) -> None:
        self.t = start

    def tick(self) -> int:
        self.t += 1
        return self.t

    def observe(self, t: int) -> None:
        self.t = max(self.t, t)


def set_op(
    client: str,
    seq: int,
    cell: str,
    content: str,
    ts: int | None = None,
    sheet: str = "s1",
) -> Operation:
    return Operation(
        OpId(client, seq), seq if ts is None else ts, sheet, "set", parse_addr(cell), content
    )


def clear_op(
    client: str, seq: int, cell: str, ts: int | None = None, sheet: str = "s1"
) -> Operation:
    return Operation(
        OpId(client, seq), seq if ts is None else ts, sheet, "clear", parse_addr(cell)
    )


def grant_op(
    owner: str,
    seq: int,
    target: str,
    cell: str,
    ts: int | None = None,
    sheet: str = "s1",
) -> Operation:
    return Operation(
        OpId(owner, seq),
        seq if ts is None else ts,
        sheet,
        "grant",
        parse_addr(cell),
        target_client=target,
    )


def revoke_op(
    owner: str,
    seq: int,
    target: str,
    cell: str,
    ts: int | None = None,
    sheet: str = "s1",
) -> Operation:
    return Operation(
        OpId(owner, seq),
        seq if ts is None else ts,
        sheet,
        "revoke",
        parse_addr(cell),
        target_client=target,
    )


def readonly_op(
    owner: str, seq: int, value: bool, ts: int | None = None, sheet: str = "s1"
) -> Operation:
    return Operation(
        OpId(owner, seq),
        seq if ts is None else ts,
        sheet,
        "sheet_readonly",
        readonly=value,
    )


@pytest.fixture
def tmp_service(tmp_path):
    service = SpreadsheetService(WALStorage(tmp_path / "data"))
    service.create_sheet("s1", "owner")
    return service


def raw_of(service, sheet_id: str, cell: str) -> str | None:
    return service.snapshot(sheet_id)["cells"].get(cell, {}).get("raw")


def value_of(service, sheet_id: str, cell: str):
    return service.snapshot(sheet_id)["cells"].get(cell, {}).get("value")
