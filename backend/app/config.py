"""Application configuration.

Everything is environment-driven so the same code runs locally and on Vercel.
"""

import os
from pathlib import Path

# Repo root is two levels up from this file (backend/app/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

# Data directory (extracted corpus + structured JSON). Overridable for tests/deploys.
DATA_DIR = Path(os.environ.get("PARCELPILOT_DATA_DIR", REPO_ROOT / "data"))
CORPUS_DIR = DATA_DIR / "corpus"
STRUCTURED_DIR = DATA_DIR / "structured"

# Claude model powering the agent. Sonnet 5 by default for cost; override with
# PARCELPILOT_MODEL (e.g. claude-opus-5) without code changes.
MODEL = os.environ.get("PARCELPILOT_MODEL", "claude-sonnet-5")
MAX_TOKENS = int(os.environ.get("PARCELPILOT_MAX_TOKENS", "8192"))

# Hard cap on model turns per user message (each turn may contain several tool calls).
MAX_AGENT_TURNS = int(os.environ.get("PARCELPILOT_MAX_AGENT_TURNS", "12"))

# Secret for signing session tokens and pending-action payloads. Mocked auth:
# a real deployment would use a proper identity provider, but signatures keep
# the demo honest (tokens and pending actions cannot be forged client-side).
SESSION_SECRET = os.environ.get("PARCELPILOT_SESSION_SECRET", "parcelpilot-demo-secret")

# Business-hours calendar assumption (documented in the architecture note):
# Monday-Friday, 09:00-18:00 IST. The dataset snapshot (2026-08-16) is a Sunday,
# which is exactly why business-time vs 24x7 targets diverge in this data.
BUSINESS_DAY_START_HOUR = 9
BUSINESS_DAY_END_HOUR = 18
BUSINESS_HOURS_PER_DAY = BUSINESS_DAY_END_HOUR - BUSINESS_DAY_START_HOUR
