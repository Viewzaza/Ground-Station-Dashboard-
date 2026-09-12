"""Single-process dev server: API and frontend on one origin, no Docker.

Production serves the frontend from Caddy and the API from uvicorn. That needs
containers, which is a slow loop on a laptop — and on Windows, Docker Desktop
may not even be running. This runner mounts the same FastAPI app and serves the
static frontend beside it, so the browser still sees one origin and no CORS.

    python tools/dev_server.py            # http://localhost:8000

The camera panel will report the bridge as unreachable unless go2rtc is running
separately; that is a real state the UI is built to show, not a failure of this
runner.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("GS_MOCK", "1")
os.environ.setdefault("GS_DATA_DIR", str(ROOT / "backend" / "data"))
os.environ.setdefault("GS_LOG_LEVEL", "info")

import uvicorn                                     # noqa: E402
from fastapi.staticfiles import StaticFiles        # noqa: E402

from app.main import app                           # noqa: E402

# Mounted last so every /api route still wins.
app.mount("/", StaticFiles(directory=str(ROOT / "frontend"), html=True), name="frontend")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"dashboard  http://localhost:{port}")
    print(f"api docs   http://localhost:{port}/api/docs")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
