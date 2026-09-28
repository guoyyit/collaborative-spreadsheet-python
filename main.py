"""Development entry point: ``uv run uvicorn main:app`` or ``uv run python main.py``."""

from spreadsheet.api import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
