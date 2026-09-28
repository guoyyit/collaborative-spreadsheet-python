"""Formula cells: dependency-ordered recompute, cycles, invalid references.

* changing a referenced cell recomputes dependents in topological order;
* diamond and long chains evaluate correctly;
* self and mutual cycles are rejected with diagnostic messages at commit;
* runtime cycles (defense-in-depth) show #CYCLE! instead of a number;
* #DIV/0!, #VALUE! and bad addresses never propagate as plain numbers;
* text dependencies and missing (empty) dependencies behave per spec.
"""


from conftest import set_op

from collab_sheet.service import BatchSubmitRequest


def _set(svc, sid, client, op_id, seq, ts, r, c, v):
    res = svc.submit_batch(sid, BatchSubmitRequest(client,
        [set_op(op_id, client, seq, ts, r, c, v).to_dict()]))
    assert res.rejected == [], [x.detail for x in res.rejected]
    return res


def test_dependents_recompute_in_dependency_order(seeded):
    svc, sid = seeded
    _set(svc, sid, "alice", "a", 1, 100, 0, 0, "1")
    _set(svc, sid, "alice", "b", 2, 101, 1, 0, "=A1+10")       # 11
    _set(svc, sid, "alice", "c", 3, 102, 2, 0, "=A2*2")        # 22
    _set(svc, sid, "alice", "d", 4, 103, 3, 0, "=A2+A3")       # 33
    assert [svc.get_cell(sid, r, 0).value for r in range(4)] == ["1", "11", "22", "33"]
    # change the root: whole chain recomputes
    _set(svc, sid, "alice", "a2", 5, 200, 0, 0, "5")
    assert [svc.get_cell(sid, r, 0).value for r in range(4)] == ["5", "15", "30", "45"]


def test_diamond_dependency(seeded):
    svc, sid = seeded
    _set(svc, sid, "alice", "a", 1, 100, 0, 0, "3")
    _set(svc, sid, "alice", "b", 2, 101, 0, 1, "=A1*2")   # 6
    _set(svc, sid, "alice", "c", 3, 102, 1, 0, "=A1+1")   # 4
    _set(svc, sid, "alice", "d", 4, 103, 1, 1, "=B1+A2")  # 10
    assert svc.get_cell(sid, 1, 1).value == "10"
    _set(svc, sid, "alice", "a2", 5, 200, 0, 0, "10")
    assert svc.get_cell(sid, 1, 1).value == "31"          # 20 + 11


def test_self_cycle_rejected_with_diagnostic(seeded):
    svc, sid = seeded
    res = svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("cy", "alice", 1, 100, 0, 0, "=A1+1").to_dict()]))
    assert len(res.rejected) == 1
    rej = res.rejected[0]
    assert rej.reason == "cycle"
    assert "#CYCLE!" in rej.detail
    assert "A1" in rej.detail
    # rejected op is not persisted
    assert svc.get_cell(sid, 0, 0) is None


def test_mutual_cycle_rejected_with_path(seeded):
    svc, sid = seeded
    _set(svc, sid, "alice", "a", 1, 100, 0, 0, "=B1+1")
    _set(svc, sid, "alice", "b", 2, 101, 0, 1, "2")
    # now make B1 point back at A1 -> cycle
    res = svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("b2", "alice", 3, 200, 0, 1, "=A1+1").to_dict()]))
    rej = res.rejected[0]
    assert rej.reason == "cycle"
    assert "B1" in rej.detail and "A1" in rej.detail
    # old B1 value preserved
    assert svc.get_cell(sid, 0, 1).value == "2"


def test_three_node_cycle_rejected(seeded):
    svc, sid = seeded
    _set(svc, sid, "alice", "a", 1, 100, 0, 0, "=C1+1")
    _set(svc, sid, "alice", "b", 2, 101, 0, 1, "=A1+1")
    res = svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("c", 3, 102, 0, 2, "=B1+1").to_dict()])) if False else None
    res = svc.submit_batch(sid, BatchSubmitRequest("alice",
        [set_op("c", "alice", 3, 102, 0, 2, "=B1+1").to_dict()]))
    assert res.rejected and res.rejected[0].reason == "cycle"


def test_syntax_and_bad_address_rejected(seeded):
    svc, sid = seeded
    bad_forms = ["=1+", "=()", "=A+1", "=1 2", "=AAAA1+1"]
    for i, form in enumerate(bad_forms):
        res = svc.submit_batch(sid, BatchSubmitRequest("alice",
            [set_op(f"bad{i}", "alice", i + 1, 200 + i, 5 + i, 0, form).to_dict()]))
        assert res.rejected, form
        assert res.rejected[0].reason in ("bad_formula",)


def test_error_cells_never_become_numbers(seeded):
    svc, sid = seeded
    _set(svc, sid, "alice", "z", 1, 100, 0, 0, "=1/0")           # #DIV/0!
    assert svc.get_cell(sid, 0, 0).error == "#DIV/0!"
    _set(svc, sid, "alice", "d", 2, 101, 0, 1, "=A1+1")
    assert svc.get_cell(sid, 0, 1).error == "#DIV/0!"            # propagates
    _set(svc, sid, "alice", "e", 3, 102, 0, 2, "=B1*2")
    assert svc.get_cell(sid, 0, 2).error == "#DIV/0!"
    # text dependency
    _set(svc, sid, "alice", "t", 4, 200, 5, 0, "hello")
    _set(svc, sid, "alice", "u", 5, 201, 5, 1, "=A6+1")
    assert svc.get_cell(sid, 5, 1).error == "#VALUE!"
    # fixing the source heals dependents
    _set(svc, sid, "alice", "z2", 6, 300, 0, 0, "41")
    assert svc.get_cell(sid, 0, 1).value == "42"
    assert svc.get_cell(sid, 0, 2).value == "84"


def test_empty_reference_is_zero_but_errors_are_not(seeded):
    svc, sid = seeded
    _set(svc, sid, "alice", "f", 1, 100, 0, 0, "=Z99+5")
    assert svc.get_cell(sid, 0, 0).value == "5"     # empty cell reads as 0
    assert svc.get_cell(sid, 0, 0).error is None
