"""Formula dependency propagation and circular reference rejection."""

from __future__ import annotations

from .conftest import set_op, value_of


def test_dependencies_recompute_in_dependency_order(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [
            set_op("owner", 1, "A1", "2", ts=1),
            set_op("owner", 2, "B1", "=A1+1", ts=2),
            set_op("owner", 3, "C1", "=B1*10", ts=3),
            set_op("owner", 4, "D1", "=SUM(A1:C1)", ts=4),
        ],
        "setup",
    )
    assert value_of(svc, "s1", "B1") == 3
    assert value_of(svc, "s1", "C1") == 30
    assert value_of(svc, "s1", "D1") == 35
    svc.submit("s1", "owner", [set_op("owner", 5, "A1", "5", ts=5)], "update")
    assert value_of(svc, "s1", "B1") == 6
    assert value_of(svc, "s1", "C1") == 60
    assert value_of(svc, "s1", "D1") == 71


def test_direct_cycle_is_stably_rejected_with_diagnostic(tmp_service):
    svc = tmp_service
    r = svc.submit("s1", "owner", [set_op("owner", 1, "A1", "=A1+1", ts=1)], "cyc")
    assert not r.rejected  # accepted into log, but evaluates to an error
    v = value_of(svc, "s1", "A1")
    assert isinstance(v, dict) and v["error"] == "CIRCULAR"
    assert "A1" in v["message"]


def test_indirect_cycle_marks_all_members_and_dependents(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [
            set_op("owner", 1, "A1", "=B1+1", ts=1),
            set_op("owner", 2, "B1", "=C1+1", ts=2),
            set_op("owner", 3, "C1", "=A1+1", ts=3),
            set_op("owner", 4, "D1", "=C1*2", ts=4),
        ],
        "cyc",
    )
    for cell in ("A1", "B1", "C1", "D1"):
        v = value_of(svc, "s1", cell)
        assert isinstance(v, dict) and v["error"] == "CIRCULAR", (cell, v)


def test_breaking_a_cycle_restores_values_deterministically(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [
            set_op("owner", 1, "A1", "=B1+1", ts=1),
            set_op("owner", 2, "B1", "=A1+1", ts=2),
        ],
        "cyc",
    )
    assert value_of(svc, "s1", "A1")["error"] == "CIRCULAR"
    # overwrite A1 with a literal; B1 now resolves, regardless of replay path
    svc.submit("s1", "owner", [set_op("owner", 3, "A1", "10", ts=3)], "fix")
    assert value_of(svc, "s1", "A1") == 10
    assert value_of(svc, "s1", "B1") == 11


def test_missing_reference_is_an_error_not_a_number(tmp_service):
    svc = tmp_service
    svc.submit("s1", "owner", [set_op("owner", 1, "A1", "=Z99+1", ts=1)], "miss")
    v = value_of(svc, "s1", "A1")
    assert isinstance(v, dict) and v["error"] == "REF"
    # error propagates through a dependent formula as an error, never a value
    svc.submit("s1", "owner", [set_op("owner", 2, "B1", "=A1+5", ts=2)], "d")
    v2 = value_of(svc, "s1", "B1")
    assert isinstance(v2, dict) and v2["error"] == "REF"
    # clearing the referenced cell also produces REF
    svc.submit(
        "s1",
        "owner",
        [
            set_op("owner", 3, "C1", "7", ts=3),
            set_op("owner", 4, "D1", "=C1", ts=4),
        ],
        "ok",
    )
    assert value_of(svc, "s1", "D1") == 7
    from spreadsheet.celladdr import parse_addr
    from spreadsheet.operations import Operation, OpId
    svc.submit(
        "s1",
        "owner",
        [Operation(OpId("owner", 5), 5, "s1", "clear", parse_addr("C1"))],
        "clr",
    )
    assert value_of(svc, "s1", "D1")["error"] == "REF"


def test_syntax_error_is_diagnostic_and_does_not_crash(tmp_service):
    svc = tmp_service
    svc.submit("s1", "owner", [set_op("owner", 1, "A1", "=1+", ts=1)], "bad")
    v = value_of(svc, "s1", "A1")
    assert isinstance(v, dict) and v["error"] == "SYNTAX"
    # other cells keep working
    svc.submit("s1", "owner", [set_op("owner", 2, "B1", "=2*3", ts=2)], "good")
    assert value_of(svc, "s1", "B1") == 6


def test_division_by_zero_propagates_as_error(tmp_service):
    svc = tmp_service
    svc.submit(
        "s1",
        "owner",
        [
            set_op("owner", 1, "A1", "0", ts=1),
            set_op("owner", 2, "B1", "=1/A1", ts=2),
        ],
        "dz",
    )
    v = value_of(svc, "s1", "B1")
    assert isinstance(v, dict) and v["error"] == "DIV0"
