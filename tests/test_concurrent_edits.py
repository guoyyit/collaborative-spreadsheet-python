"""Concurrent edits to the same and different cells.

Merge rule under test: per-cell last-writer-wins on the deterministic total
order (timestamp, client_id, seq); identical op sets converge to identical
final states regardless of arrival order; every cell records its winner.
"""

from __future__ import annotations

import itertools

from .conftest import Clock, grant_op, raw_of, set_op


def _two_clients(service, clock: Clock):
    service.submit(
        "s1",
        "owner",
        [
            grant_op("owner", 1, "c1", "A1", ts=clock.tick()),
            grant_op("owner", 2, "c2", "A1", ts=clock.tick()),
        ],
        "grants-a1",
    )


def test_same_cell_conflict_resolves_by_total_order(tmp_service):
    svc = tmp_service
    clk = Clock()
    _two_clients(svc, clk)
    # concurrent edits: c1 writes at logical time 5, c2 at 7 -> c2 wins
    svc.submit("s1", "c1", [set_op("c1", 1, "A1", "one", ts=5)], "b1")
    svc.submit("s1", "c2", [set_op("c2", 1, "A1", "two", ts=7)], "b2")
    assert raw_of(svc, "s1", "A1") == "two"
    assert svc.snapshot("s1")["cells"]["A1"]["written_by"] == "c2:1"
    # Equal timestamps tie-break lexicographically; the *later* op in the
    # total order wins, so the larger client id ("c2") wins deterministically.
    svc.submit("s1", "c2", [set_op("c2", 2, "A1", "tie-two", ts=9)], "b3")
    svc.submit("s1", "c1", [set_op("c1", 2, "A1", "tie-one", ts=9)], "b4")
    assert raw_of(svc, "s1", "A1") == "tie-two"
    assert svc.snapshot("s1")["cells"]["A1"]["written_by"] == "c2:2"


def test_conflict_outcome_independent_of_arrival_order(tmp_path):
    """Same set of ops delivered in every batch order yields one final state."""
    from spreadsheet.service import SpreadsheetService
    from spreadsheet.storage import WALStorage

    grants = [
        grant_op("owner", 1, "c1", "A1", ts=1),
        grant_op("owner", 2, "c2", "A1", ts=2),
    ]
    writes = [
        set_op("owner", 3, "A1", "seed", ts=3),
        set_op("c1", 1, "A1", "c1-first", ts=5),
        set_op("c1", 2, "A1", "c1-second", ts=8),
        set_op("c2", 1, "A1", "c2-first", ts=6),
    ]
    batches = [
        ("b0", writes[0:1]),
        ("b1", writes[1:3]),
        ("b2", writes[3:4]),
    ]
    finals = set()
    winners = set()
    for i, perm in enumerate(itertools.permutations(batches)):
        svc = SpreadsheetService(WALStorage(tmp_path / f"case-{i}"))
        svc.create_sheet("s1", "owner")
        svc.submit("s1", "owner", grants, "grants")
        for bid, ops in perm:
            cid = ops[0].id.client_id
            res = svc.submit("s1", cid, ops, bid)
            assert not res.rejected
        cell = svc.snapshot("s1")["cells"]["A1"]
        finals.add(cell["raw"])
        winners.add(cell["written_by"])
    assert finals == {"c1-second"}, finals
    assert winners == {"c1:2"}, winners


def test_edits_to_different_regions_both_preserved(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [
            grant_op("owner", 1, "c1", "A1", ts=1),
            grant_op("owner", 2, "c2", "B2", ts=2),
        ],
        "grants",
    )
    svc.submit("s1", "c1", [set_op("c1", 1, "A1", "left", ts=5)], "b1")
    svc.submit("s1", "c2", [set_op("c2", 1, "B2", "right", ts=6)], "b2")
    assert raw_of(svc, "s1", "A1") == "left"
    assert raw_of(svc, "s1", "B2") == "right"


def test_winner_attribution_is_stable_and_traceable(tmp_service):
    svc = tmp_service
    clk = Clock()
    _two_clients(svc, clk)
    svc.submit("s1", "c1", [set_op("c1", 1, "A1", "=1+1", ts=5)], "b1")
    svc.submit("s1", "c2", [set_op("c2", 1, "A1", "=2+2", ts=7)], "b2")
    cell = svc.snapshot("s1")["cells"]["A1"]
    assert cell["written_by"] == "c2:1"
    assert cell["value"] == 4


def test_parallel_threads_produce_deterministic_consistent_state(tmp_service, monkeypatch):
    """Many threads submit batches concurrently: no lost ops, one stable state."""
    import threading

    from spreadsheet.celladdr import parse_addr
    from spreadsheet.operations import Operation, OpId

    svc = tmp_service
    n_clients = 8
    per_client = 10
    # owner grants every client its own row
    grants = [
        Operation(
            OpId("owner", i + 1),
            i + 1,
            "s1",
            "grant",
            parse_addr(f"A{i + 1}"),
            target_client=f"c{i}",
        )
        for i in range(n_clients)
    ]
    svc.submit("s1", "owner", grants, "grants")

    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            cid = f"c{i}"
            ops = [
                Operation(
                    OpId(cid, s + 1),
                    100 + i * 100 + s,
                    "s1",
                    "set",
                    parse_addr(f"A{i + 1}"),
                    f"{cid}-{s}",
                )
                for s in range(per_client)
            ]
            svc.submit("s1", cid, ops, f"batch-{cid}")
        except Exception as exc:  # noqa: BLE001 - test failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    cells = svc.snapshot("s1")["cells"]
    assert len(cells) == n_clients
    # each client's own largest timestamp won its own row
    for i in range(n_clients):
        addr = f"A{i + 1}"
        cid = f"c{i}"
        assert cells[addr]["raw"] == f"{cid}-{per_client - 1}"
        assert cells[addr]["written_by"] == f"{cid}:{per_client}"
