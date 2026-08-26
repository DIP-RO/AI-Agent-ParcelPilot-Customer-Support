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

# ---------------------------------------------------------------------------
# Local fallback (SLM): used automatically when no Anthropic key is configured
# (e.g. a free-tier deploy with no budget for API calls), so the app is never
# fully down. See app/slm.py and app/fallback_agent.py.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
HAS_ANTHROPIC_KEY = bool(ANTHROPIC_API_KEY)

# GGUF model path. On Docker/Render it's baked into the image at build time
# (scripts/fetch_slm_model.py) and this just points at that file. On Vercel
# the deployment bundle is read-only, so app/slm.py lazily downloads the same
# file into /tmp (writable, and persists across warm invocations of the same
# instance) on first use instead -- detected via Vercel's own VERCEL env var.
# Small on purpose either way: this must fit comfortably in a 512MB-RAM
# free-tier container alongside the rest of the app.
_slm_default_dir = Path("/tmp") if os.environ.get("VERCEL") else (DATA_DIR / "models")
SLM_MODEL_PATH = Path(os.environ.get("PARCELPILOT_SLM_MODEL_PATH", _slm_default_dir / "slm.gguf"))
SLM_MODEL_URL = os.environ.get(
    "PARCELPILOT_SLM_MODEL_URL",
    "https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/main/SmolLM2-135M-Instruct-Q8_0.gguf",
)
SLM_CONTEXT_TOKENS = int(os.environ.get("PARCELPILOT_SLM_CTX", "1024"))
SLM_MAX_NEW_TOKENS = int(os.environ.get("PARCELPILOT_SLM_MAX_TOKENS", "64"))
SLM_THREADS = int(os.environ.get("PARCELPILOT_SLM_THREADS", "2"))
# Hard wall-clock ceiling on a single generation call (prefill + decode), via
# llama.cpp's stopping_criteria hook -- not a killed thread, so a host too
# slow to finish degrades to a clean partial (or, worst case, template)
# answer, never a hang. Measured on a Render-free-equivalent 0.1 vCPU
# container: prefill and decode both run at only ~0.5-0.6 tokens/sec, so even
# a short prompt's prefill can take the better part of a minute -- this is
# deliberately generous (paired with the fallback_agent SSE keepalive, which
# was verified to hold the connection open past two minutes) rather than
# tight, because a too-tight deadline would make the SLM path fire the
# instant template every time and never actually get to speak.
SLM_TIMEOUT_SECONDS = float(os.environ.get("PARCELPILOT_SLM_TIMEOUT_S", "90"))

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

# Marker prefix for server-generated "trusted" notes injected into the chat
# (e.g. action-execution confirmations). Shared by app/agent.py and
# app/fallback_agent.py so both refuse to treat user text starting with this
# prefix as a trusted platform message.
SYSTEM_NOTE_PREFIX = "[SYSTEM NOTE"
