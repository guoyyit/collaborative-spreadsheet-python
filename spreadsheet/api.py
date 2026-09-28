"""HTTP API (FastAPI) exposing the collaborative spreadsheet service.

Endpoints
---------
POST /sheets/{sheet_id}                      owner creates a sheet
GET  /sheets/{sheet_id}                      full materialized snapshot
POST /sheets/{sheet_id}/batches/{batch_id}   submit a batch (offline replay)
GET  /sheets/{sheet_id}/sync                 incremental sync (?vector=json)
POST /sheets/{sheet_id}/undo                 per-client undo
POST /sheets/{sheet_id}/redo                 per-client redo

Config: data directory via SHEET_DATA_DIR env var (default ./.sheet_data).
"""

from __future__ import annotations

import json
import os
import tempfile

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from spreadsheet.errors import (
    DuplicateSubmitError,
    NotFoundError,
    PermissionDeniedError,
)
from spreadsheet.operations import Operation
from spreadsheet.service import SpreadsheetService
from spreadsheet.storage import WALStorage
from spreadsheet.sync import VersionVector


class OpIn(BaseModel):
    client_id: str
    seq: int
    ts: int = 0
    kind: str
    cell: str | None = None
    content: str | None = None
    target_client: str | None = Field(default=None, alias="target")
    readonly: bool | None = None

    def to_operation(self, sheet_id: str) -> Operation:
        data = {
            "client_id": self.client_id,
            "seq": self.seq,
            "ts": self.ts,
            "sheet": sheet_id,
            "kind": self.kind,
        }
        if self.cell is not None:
            data["cell"] = self.cell
        if self.content is not None:
            data["content"] = self.content
        if self.target_client is not None:
            data["target"] = self.target_client
        if self.readonly is not None:
            data["readonly"] = self.readonly
        return Operation.from_dict(data)


class BatchIn(BaseModel):
    ops: list[OpIn]


class ClientIn(BaseModel):
    client_id: str


def create_app(data_dir: str | None = None) -> FastAPI:
    data_dir = data_dir or os.environ.get("SHEET_DATA_DIR") or os.path.join(
        tempfile.gettempdir(), "collab-sheets"
    )
    service = SpreadsheetService(WALStorage(data_dir))
    app = FastAPI(title="Collaborative Spreadsheet", version="0.1.0")
    app.state.service = service
    app.state.data_dir = data_dir

    def svc() -> SpreadsheetService:
        return app.state.service

    @app.post("/sheets/{sheet_id}")
    def create_sheet(sheet_id: str, body: ClientIn):
        try:
            svc().create_sheet(sheet_id, body.client_id)
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="sheet already exists") from exc
        return {"sheet_id": sheet_id, "owner": body.client_id}

    @app.get("/sheets/{sheet_id}")
    def get_sheet(sheet_id: str):
        try:
            return svc().snapshot(sheet_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/sheets/{sheet_id}/batches/{batch_id}")
    def submit_batch(sheet_id: str, batch_id: str, body: BatchIn):
        if not body.ops:
            raise HTTPException(status_code=400, detail="empty batch")
        client_ids = {op.client_id for op in body.ops}
        if len(client_ids) != 1:
            raise HTTPException(status_code=400, detail="one client per batch")
        client_id = client_ids.pop()
        try:
            ops = [op.to_operation(sheet_id) for op in body.ops]
            result = svc().submit(sheet_id, client_id, ops, batch_id)
        except DuplicateSubmitError as exc:
            raise HTTPException(status_code=409, detail="duplicate batch") from exc
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        svc().note_user_ops(sheet_id, client_id, result.accepted)
        return {
            "accepted": [op.to_dict() for op in result.accepted],
            "rejected": [
                {"op": op.to_dict(), "reason": reason}
                for op, reason in result.rejected
            ],
            "clock": result.clock,
        }

    @app.get("/sheets/{sheet_id}/sync")
    def sync_sheet(sheet_id: str, vector: str = "{}"):
        try:
            known = VersionVector.from_dict(json.loads(vector))
            ops = svc().sync(sheet_id, known)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="bad vector") from exc
        return {
            "ops": [op.to_dict() for op in ops],
            "version": svc().version_vector(sheet_id).to_dict(),
        }

    @app.post("/sheets/{sheet_id}/undo")
    def undo(sheet_id: str, body: ClientIn):
        try:
            op = svc().undo(sheet_id, body.client_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"op": op.to_dict() if op else None}

    @app.post("/sheets/{sheet_id}/redo")
    def redo(sheet_id: str, body: ClientIn):
        try:
            op = svc().redo(sheet_id, body.client_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"op": op.to_dict() if op else None}

    return app


app = create_app()
