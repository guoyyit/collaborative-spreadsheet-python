"""End-to-end HTTP API tests including offline replay and restart recovery."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spreadsheet.api import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    return TestClient(app)


def _set(cid, seq, cell, content, ts=None):
    op = {
        "client_id": cid,
        "seq": seq,
        "kind": "set",
        "cell": cell,
        "content": content,
    }
    if ts is not None:
        op["ts"] = ts
    return op


def test_full_collaboration_flow_over_http(client):
    # owner creates sheet
    r = client.post("/sheets/s1", json={"client_id": "owner"})
    assert r.status_code == 200
    assert r.json()["owner"] == "owner"
    # duplicate create -> 409
    assert client.post("/sheets/s1", json={"client_id": "owner"}).status_code == 409

    # owner seeds cells and a formula, and grants c1 access to A1
    batch = {
        "ops": [
            {"client_id": "owner", "seq": 1, "ts": 1, "kind": "set", "cell": "A1", "content": "3"},
            {"client_id": "owner", "seq": 2, "ts": 2, "kind": "set", "cell": "B1", "content": "=A1*2"},
            {"client_id": "owner", "seq": 3, "ts": 3, "kind": "grant", "cell": "A1", "target": "c1"},
        ]
    }
    r = client.post("/sheets/s1/batches/seed", json=batch)
    assert r.status_code == 200 and not r.json()["rejected"]

    snap = client.get("/sheets/s1").json()
    assert snap["cells"]["B1"]["value"] == 6

    # c1 edits A1; formula B1 re-propagates
    r = client.post(
        "/sheets/s1/batches/c1a",
        json={"ops": [_set("c1", 1, "A1", "10", ts=5)]},
    )
    assert r.status_code == 200
    assert client.get("/sheets/s1").json()["cells"]["B1"]["value"] == 20

    # unauthorized client is rejected at the engine level (200 + rejected)
    r = client.post(
        "/sheets/s1/batches/c2bad",
        json={"ops": [_set("c2", 1, "A1", "hack", ts=6)]},
    )
    assert r.status_code == 200 and len(r.json()["rejected"]) == 1
    assert client.get("/sheets/s1").json()["cells"]["A1"]["raw"] == "10"

    # duplicate batch id -> 409
    r = client.post("/sheets/s1/batches/c1a", json={"ops": [_set("c1", 1, "A1", "10", ts=5)]})
    assert r.status_code == 409


def test_incremental_sync_endpoint(client):
    client.post("/sheets/s1", json={"client_id": "owner"})
    client.post("/sheets/s1/batches/b1", json={"ops": [_set("owner", 1, "A1", "1", 1)]})
    client.post("/sheets/s1/batches/b2", json={"ops": [_set("owner", 2, "A2", "2", 2)]})
    r = client.get("/sheets/s1/sync", params={"vector": '{"owner": 1}'})
    ops = r.json()["ops"]
    assert len(ops) == 1 and ops[0]["seq"] == 2
    assert r.json()["version"] == {"owner": 2}
    # full sync
    assert len(client.get("/sheets/s1/sync").json()["ops"]) == 2


def test_offline_replay_then_undo_redo_over_http(client):
    client.post("/sheets/s1", json={"client_id": "owner"})
    client.post(
        "/sheets/s1/batches/seed",
        json={
            "ops": [
                _set("owner", 1, "A1", "base", 1),
                {"client_id": "owner", "seq": 2, "ts": 2, "kind": "grant", "cell": "A1", "target": "c1"},
                {"client_id": "owner", "seq": 4, "ts": 4, "kind": "grant", "cell": "C1", "target": "c1"},
            ]
        },
    )
    # concurrent owner edit arrives while c1 is offline
    client.post("/sheets/s1/batches/online", json={"ops": [_set("owner", 3, "A1", "online", 10)]})
    # c1 replays stale A1 (loses) + fresh C1 (wins) in one batch
    r = client.post(
        "/sheets/s1/batches/offline",
        json={
            "ops": [
                _set("c1", 1, "A1", "stale", 6),
                _set("c1", 2, "C1", "mine", 7),
            ]
        },
    )
    assert not r.json()["rejected"]
    snap = client.get("/sheets/s1").json()["cells"]
    assert snap["A1"]["raw"] == "online"
    assert snap["C1"]["raw"] == "mine"
    # c1 undoes its C1 edit
    assert client.post("/sheets/s1/undo", json={"client_id": "c1"}).status_code == 200
    assert "C1" not in client.get("/sheets/s1").json()["cells"]
    # redo brings it back
    assert client.post("/sheets/s1/redo", json={"client_id": "c1"}).status_code == 200
    assert client.get("/sheets/s1").json()["cells"]["C1"]["raw"] == "mine"


def test_state_persists_across_app_restart(tmp_path):
    d = str(tmp_path / "data")
    c1 = TestClient(create_app(d))
    c1.post("/sheets/s1", json={"client_id": "owner"})
    c1.post("/sheets/s1/batches/b1", json={"ops": [_set("owner", 1, "A1", "durable", 1)]})
    # new app instance = new process with the same data directory
    c2 = TestClient(create_app(d))
    assert c2.get("/sheets/s1").json()["cells"]["A1"]["raw"] == "durable"
    assert c2.get("/sheets/s1").status_code == 200
    # unknown sheet -> 404
    assert c2.get("/sheets/nope").status_code == 404


def test_circular_reference_surfaced_in_snapshot(client):
    client.post("/sheets/s1", json={"client_id": "owner"})
    client.post(
        "/sheets/s1/batches/cyc",
        json={
            "ops": [
                _set("owner", 1, "A1", "=B1+1", 1),
                _set("owner", 2, "B1", "=A1+1", 2),
            ]
        },
    )
    cell = client.get("/sheets/s1").json()["cells"]["A1"]
    assert cell["value"]["error"] == "CIRCULAR"
    assert "circular" in cell["value"]["message"].lower()
