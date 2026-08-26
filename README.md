# ParcelPilot Support Copilot

An AI support system for ParcelPilot (B2B logistics) with **two user contexts**:

- **Customer-facing chatbot** — answers a customer's questions about their own orders, tickets, entitlements, cancellations, credits and SLAs, and escalates to humans with confirmation.
- **Internal ops copilot + Ops Radar** — lets ParcelPilot staff investigate any account, re-verify past answers, take actions (escalate / update tickets / tasks / credits), and see a proactive radar of SLA breaches, known-issue clusters and unactioned entitlements.

Built for the CalQuity AI Engineer assessment. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PRODUCT_NOTE.md](docs/PRODUCT_NOTE.md), [docs/AI_USAGE.md](docs/AI_USAGE.md).

## Highlights

- **Deterministic policy engines** for cancellation fees, service credits and business-hours SLA math — the LLM never does money/date arithmetic; every verdict carries a rule trace with clause citations (e.g. *"Northstar Enterprise Agreement §2 OVERRIDES SOP v4 §1"*).
- **Access control in the data/tool layer**, not the prompt: HMAC-signed persona tokens; customers physically cannot retrieve other accounts' rows or agreements (the model never sees them).
- **Structural confirmation for actions**: agent tools only *prepare* a signed pending action; execution happens on a separate endpoint wired to the UI's Confirm button. The model cannot execute anything by itself.
- **Source-reliability handling**: authority-tiered retrieval (agreement > policy/SOP > product docs), deprecated-policy quarantine, "may be incorrect" annotations on historical ticket resolutions, and stale-status caveats driven by live known issues (KI-211).
- **Local SLM fallback with no API key**: if `ANTHROPIC_API_KEY` is unset (e.g. a free-tier deploy with no API budget, or a live outage), the app automatically serves a narrower agent backed by a small on-device model (~135M params, `llama-cpp-python`) instead of just erroring — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#local-fallback-mode-no-api-key).
- **94 backend tests** covering every order/ticket in the pack, the designed traps, access control (including path-traversal and id-enumeration hardening), the confirmation flow, and the no-API-key fallback router.

## Run locally

Prereqs: Python 3.11+, Node 18+. An Anthropic API key is **optional** — without
one the chat runs on the local SLM fallback instead (see below).

```bash
# 1. Backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=sk-ant-...   # omit this to run the free local-SLM fallback instead
.venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8000

# 2. Frontend (second terminal)
cd frontend
npm install
npm run dev            # http://localhost:5173 (proxies /api to :8000)
```

Or a single service: `cd frontend && npm run build`, then the FastAPI server also serves the built UI at http://localhost:8000.

### Tests

```bash
.venv/bin/python -m pytest backend/tests   # no API key needed
```

## Local SLM fallback (no API key)

If `ANTHROPIC_API_KEY` is unset, `/api/chat` automatically routes to
`app/fallback_agent.py` — a plain-Python keyword/entity router over the exact
same access-controlled tools, with a small on-device model (`app/slm.py`,
~135M params via `llama-cpp-python`) only phrasing the result. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#local-fallback-mode-no-api-key)
for the full design and the latency numbers that shaped it.

```bash
.venv/bin/pip install --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu llama-cpp-python==0.3.34
.venv/bin/python scripts/fetch_slm_model.py   # downloads data/models/slm.gguf (~145MB, gitignored)
```

The Docker image (below) fetches this model automatically at build time.

## Deploy (Render — free tier, includes the SLM fallback)

The repo includes a [render.yaml](render.yaml) Blueprint, so this is close to
one-click: on [Render](https://render.com), **New +** → **Blueprint** → point
it at this repo. It provisions a free Docker web service that builds the
image (frontend + backend + the fallback model), and prompts you for
`ANTHROPIC_API_KEY` — **leave it blank to run entirely on the free SLM
fallback, or set it for the full Claude-backed agent.**

Notes for the free instance type (512MB RAM / 0.1 CPU, sleeps after 15 min
idle): the app fits comfortably (~140MB resident with the fallback model
loaded), but SLM-fallback replies can take up to a couple of minutes — the
chat stream sends periodic keepalives so the connection survives the wait,
and the UI says so up front. This trade-off (and the benchmarks behind it) is
written up in the architecture note linked above.

## Deploy (Vercel)

The repo is also Vercel-ready: static frontend + Python serverless function ([api/index.py](api/index.py), [vercel.json](vercel.json)). Best suited to the Claude-backed path (serverless cold starts don't suit a resident local model well — see the architecture note).

```bash
npm i -g vercel
vercel               # from the repo root (links/creates the project)
# Optional: set ANTHROPIC_API_KEY for the Claude-backed path
#   vercel env add ANTHROPIC_API_KEY production --sensitive
# Required: set a non-default session secret
#   vercel env add PARCELPILOT_SESSION_SECRET production --sensitive
vercel --prod
```

**Live demo (local SLM fallback, no API key configured):**
https://parcelpilot-copilot.vercel.app — `/api/health` reports `mode: local_slm_fallback`, `slm_loaded: true`. First request on a cold instance may take ~60-90s while the 145 MB GGUF model downloads into `/tmp` and loads; warm invocations are faster.

Note: the mocked action log / ticket-update overlay is in-memory per serverless instance (documented trade-off — actions are mocks; a real deployment would use a database).

## Docker

```bash
docker build -t parcelpilot-copilot .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... parcelpilot-copilot
# open http://localhost:8000
# omit -e ANTHROPIC_API_KEY to exercise the local SLM fallback instead
```

The multi-stage image builds the React UI, fetches the SLM fallback model
(`scripts/fetch_slm_model.py`; skip with `--build-arg SKIP_SLM=1` for a
smaller/faster image when a key is always present), and serves everything
from one container. CI ([.github/workflows/ci.yml](.github/workflows/ci.yml))
runs the backend tests, the frontend build, and a Docker image build +
`/api/health` smoke test on every push and PR.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | if unset, chat runs on the local SLM fallback instead of erroring |
| `PARCELPILOT_MODEL` | `claude-sonnet-5` | any current Claude model id |
| `PARCELPILOT_SESSION_SECRET` | demo value | HMAC secret for tokens/pending actions |
| `PARCELPILOT_MAX_AGENT_TURNS` | `12` | tool-loop budget per message |
| `PARCELPILOT_SLM_MODEL_PATH` | `data/models/slm.gguf` | GGUF file for the fallback model |
| `PARCELPILOT_SLM_TIMEOUT_S` | `90` | hard wall-clock ceiling per fallback reply (prefill+decode) |
| `PARCELPILOT_SLM_MAX_TOKENS` | `64` | max tokens the fallback model generates per reply |

## Data pipeline

The original data pack (PDFs + XLSX) lives at the repo root. Derived, machine-readable data lives in `data/`:

- `scripts/extract_data.py` — PDFs → `data/corpus/*.txt`, workbook → `data/structured/*.json`
- `scripts/compile_contracts.py` — LLM pass that compiles each signed agreement into the structured entitlements registry (`contract_terms.json`); checked in for deterministic runs, regenerable when contracts change
- Hand-curated registries derived from the docs: `doc_registry.json` (authority tiers), `policy_defaults.json` (current policy/SOP terms), `known_issues.json`

The dataset snapshot time (2026-08-16 11:00 IST, a **Sunday** — which matters for business-hours SLAs) is read from the workbook README sheet and used as "now" everywhere.

## Demo script

A suggested 5-minute walkthrough (matching the demo video) is in [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md); the scenario matrix with expected behaviors is in [docs/EVALUATION.md](docs/EVALUATION.md).
