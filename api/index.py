"""Vercel serverless entry point — exposes the FastAPI ASGI app.

Vercel's Python runtime detects the module-level `app` object and serves it.
The repository root (with backend/ and data/) is bundled with the function.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.main import app  # noqa: E402, F401
