"""Durable storage: per-sheet directory with atomic batch files.

Layout::

    <root>/<sheet_id>/meta.json            {"owner": ...} (atomically renamed)
    <root>/<sheet_id>/batch-<seq>.json     one committed batch per file

Each batch is written to a temp file in the same directory, fsynced, then
atomically renamed. A crash before the rename leaves only an ignored temp
file; a crash mid-write leaves a temp file that is never read. Therefore
batch writes are all-or-nothing: restarted, unfinished batches are invisible.

A batch id is stored inside the file, so retried submissions with the same
batch id are detected on load and subsequent duplicate appends are no-ops.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from spreadsheet.operations import Operation

_BATCH_RE = re.compile(r"batch-(\d+)\.json$")


@dataclass(slots=True)
class StoredSheet:
    sheet_id: str
    owner: str
    batches: list[list[Operation]] = field(default_factory=list)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class WALStorage:
    def __init__(self, path: str | Path) -> None:
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)
        _fsync_dir(self.root)

    # ------------------------------------------------------------------ #
    # Sheets
    # ------------------------------------------------------------------ #
    def _sheet_dir(self, sheet_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", sheet_id):
            raise ValueError(f"unsafe sheet id: {sheet_id!r}")
        return self.root / sheet_id

    def create_sheet(self, sheet_id: str, owner: str) -> None:
        d = self._sheet_dir(sheet_id)
        if (d / "meta.json").exists():
            raise FileExistsError(sheet_id)
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(d / "meta.json", {"sheet_id": sheet_id, "owner": owner})

    def sheet_exists(self, sheet_id: str) -> bool:
        return (self._sheet_dir(sheet_id) / "meta.json").exists()

    def load_owner(self, sheet_id: str) -> str:
        meta = self._sheet_dir(sheet_id) / "meta.json"
        with meta.open(encoding="utf-8") as fh:
            return str(json.load(fh)["owner"])

    # ------------------------------------------------------------------ #
    # Batches
    # ------------------------------------------------------------------ #
    def _batch_files(self, d: Path) -> list[tuple[int, Path]]:
        found = []
        for p in d.iterdir():
            m = _BATCH_RE.search(p.name)
            if m:
                found.append((int(m.group(1)), p))
        found.sort(key=lambda x: x[0])
        return found

    def load_all(self) -> dict[str, StoredSheet]:
        result: dict[str, StoredSheet] = {}
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            meta_path = d / "meta.json"
            if not meta_path.exists():
                continue
            with meta_path.open(encoding="utf-8") as fh:
                meta = json.load(fh)
            stored = StoredSheet(sheet_id=meta["sheet_id"], owner=meta["owner"])
            for _, bp in self._batch_files(d):
                with bp.open(encoding="utf-8") as fh:
                    payload = json.load(fh)
                stored.batches.append(
                    [Operation.from_dict(op) for op in payload["ops"]]
                )
            result[stored.sheet_id] = stored
        return result

    def append_batch(self, sheet_id: str, batch_id: str, ops: list[Operation]) -> None:
        """Atomically append one batch; duplicate batch_id is ignored (idempotent).

        Raises KeyError if the sheet does not exist.
        """
        d = self._sheet_dir(sheet_id)
        if not (d / "meta.json").exists():
            raise KeyError(sheet_id)
        for _, bp in self._batch_files(d):
            with bp.open(encoding="utf-8") as fh:
                if json.load(fh).get("batch_id") == batch_id:
                    return  # already committed: duplicate delivery is a no-op
        seq = max((n for n, _ in self._batch_files(d)), default=0) + 1
        payload = {
            "batch_id": batch_id,
            "ops": [op.to_dict() for op in ops],
        }
        _atomic_write_json(d / f"batch-{seq:012d}.json", payload)

    def committed_batch_ids(self, sheet_id: str) -> set[str]:
        d = self._sheet_dir(sheet_id)
        ids: set[str] = set()
        for _, bp in self._batch_files(d):
            with bp.open(encoding="utf-8") as fh:
                ids.add(str(json.load(fh)["batch_id"]))
        return ids
