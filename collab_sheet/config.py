"""Configurable settings for the collaborative spreadsheet service.

Settings can be provided programmatically or overridden through environment
variables (``COLLAB_SHEET_DATA_DIR`` etc.).  Defaults are safe for local use;
deployment should point ``data_dir`` at durable storage.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    #: Directory holding the write-ahead log and snapshots.
    data_dir: str = "./data"
    #: fsync every committed frame. Disable only for tests / ephemeral data.
    wal_fsync: bool = True
    #: Maximum number of evaluation hops before #CYCLE! is reported.
    max_formula_depth: int = 64
    #: Maximum ops returned in one incremental-sync response.
    sync_page_size: int = 1000
    #: Default role granted to a table creator.
    default_owner_role: str = "owner"


def get_settings(
    data_dir: str | None = None,
    wal_fsync: bool | None = None,
    max_formula_depth: int | None = None,
    sync_page_size: int | None = None,
) -> Settings:
    """Build settings, falling back to env vars then built-in defaults."""
    return Settings(
        data_dir=data_dir or os.environ.get("COLLAB_SHEET_DATA_DIR", "./data"),
        wal_fsync=(
            wal_fsync
            if wal_fsync is not None
            else os.environ.get("COLLAB_SHEET_WAL_FSYNC", "1") not in ("0", "false", "False")
        ),
        max_formula_depth=max_formula_depth
        or int(os.environ.get("COLLAB_SHEET_MAX_FORMULA_DEPTH", "64")),
        sync_page_size=sync_page_size
        or int(os.environ.get("COLLAB_SHEET_SYNC_PAGE_SIZE", "1000")),
    )
