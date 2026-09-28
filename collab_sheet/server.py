"""FastAPI HTTP layer.

Endpoints
---------
POST   /tables                          create a table            {owner_id, sheet_id?}
GET    /tables/{sid}                    table meta + version
GET    /tables/{sid}/snapshot           full evaluated snapshot
GET    /tables/{sid}/cells/{row}/{col}  one cell (with provenance)
POST   /tables/{sid}/cells              set one cell
POST   /tables/{sid}/ops                submit a batch of raw ops (offline replay)
POST   /tables/{sid}/permissions        grant / revoke a role
POST   /tables/{sid}/undo               undo caller's last own edit {client_id}
POST   /tables/{sid}/redo               redo caller's last undone edit
POST   /tables/{sid}/sync               incremental sync        {known_ids, client_id, limit?}

All mutating requests accept an explicit ``ts_ms`` so offline-prepared ops
keep their causal timestamps, and an explicit ``op_id`` for end-to-end
idempotency.
"""


from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import get_settings
from .service import (
    BatchSubmitRequest,
    GrantRequest,
    SetCellRequest,
    SpreadsheetService,
    build_service,
)


# ---------------------------------------------------------------- schemas
class CreateTableIn(BaseModel):
    owner_id: str
    sheet_id: str | None = None


class SetCellIn(BaseModel):
    client_id: str
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    value: str
    op_id: str | None = None
    ts_ms: int | None = None
    seq: int | None = None


class BatchIn(BaseModel):
    client_id: str
    ops: list[dict]


class GrantIn(BaseModel):
    granter_id: str
    client_id: str
    role: str
    scope: str = "table"
    row: int = -1
    col: int = -1
    op_id: str | None = None
    ts_ms: int | None = None
    seq: int | None = None


class UndoIn(BaseModel):
    client_id: str


class SyncIn(BaseModel):
    client_id: str
    known_ids: list[str] = []
    limit: int | None = None


def create_app(service: SpreadsheetService | None = None) -> FastAPI:
    app = FastAPI(title="collaborative-spreadsheet", version="0.1.0")
    if service is None:
        settings = get_settings()
        service = build_service(settings.data_dir, fsync=settings.wal_fsync)
    app.state.service = service

    def _require_sheet(sid: str):
        snap = service.get_snapshot(sid)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"unknown sheet {sid!r}")
        return snap

    @app.post("/tables")
    def create_table(body: CreateTableIn):
        return service.create_table(body.owner_id, body.sheet_id)

    @app.get("/tables/{sid}")
    def table_meta(sid: str):
        snap = _require_sheet(sid)
        return {"sheet_id": sid, "version": snap.version}

    @app.get("/tables/{sid}/snapshot")
    def snapshot(sid: str):
        return _require_sheet(sid).to_dict()

    @app.get("/tables/{sid}/cells/{row}/{col}")
    def get_cell(sid: str, row: int, col: int):
        _require_sheet(sid)
        view = service.get_cell(sid, row, col)
        if view is None:
            return {"row": row, "col": col, "value": "", "kind": "blank",
                    "provenance": None, "error": None}
        return view.to_dict()

    @app.post("/tables/{sid}/cells")
    def set_cell(sid: str, body: SetCellIn):
        _require_sheet(sid)
        result = service.submit_set_cell(sid, SetCellRequest(
            client_id=body.client_id, row=body.row, col=body.col, value=body.value,
            op_id=body.op_id, ts_ms=body.ts_ms, seq=body.seq,
        ))
        return result.to_dict()

    @app.post("/tables/{sid}/ops")
    def submit_ops(sid: str, body: BatchIn):
        _require_sheet(sid)
        return service.submit_batch(sid, BatchSubmitRequest(client_id=body.client_id,
                                                            ops=body.ops)).to_dict()

    @app.post("/tables/{sid}/permissions")
    def grant(sid: str, body: GrantIn):
        _require_sheet(sid)
        return service.grant(sid, GrantRequest(
            granter_id=body.granter_id, client_id=body.client_id, role=body.role,
            scope=body.scope, row=body.row, col=body.col,
            op_id=body.op_id, ts_ms=body.ts_ms, seq=body.seq,
        )).to_dict()

    @app.post("/tables/{sid}/undo")
    def undo(sid: str, body: UndoIn):
        _require_sheet(sid)
        return service.undo(sid, body.client_id).to_dict()

    @app.post("/tables/{sid}/redo")
    def redo(sid: str, body: UndoIn):
        _require_sheet(sid)
        return service.redo(sid, body.client_id).to_dict()

    @app.post("/tables/{sid}/sync")
    def sync(sid: str, body: SyncIn):
        _require_sheet(sid)
        return service.sync(sid, body.client_id, body.known_ids, body.limit)

    return app


app = create_app()
