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
- **69 backend tests** covering every order/ticket in the pack, the designed traps, access control (including path-traversal and id-enumeration hardening), and the confirmation flow.

## Run locally

Prereqs: Python 3.11+, Node 18+, an Anthropic API key.

```bash
# 1. Backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=sk-ant-...
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

## Deploy (Vercel)

The repo is Vercel-ready: static frontend + Python serverless function ([api/index.py](api/index.py), [vercel.json](vercel.json)).

```bash
npm i -g vercel
vercel               # from the repo root
vercel env add ANTHROPIC_API_KEY
vercel --prod
```

Note: the mocked action log / ticket-update overlay is in-memory per serverless instance (documented trade-off — actions are mocks; a real deployment would use a database).

## Docker

```bash
docker build -t parcelpilot-copilot .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... parcelpilot-copilot
# open http://localhost:8000
```

The multi-stage image builds the React UI and serves it plus the API from one
container. CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the
backend tests, the frontend build, and a Docker image build + `/api/health`
smoke test on every push and PR.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required for chat |
| `PARCELPILOT_MODEL` | `claude-sonnet-5` | any current Claude model id |
| `PARCELPILOT_SESSION_SECRET` | demo value | HMAC secret for tokens/pending actions |
| `PARCELPILOT_MAX_AGENT_TURNS` | `12` | tool-loop budget per message |

## Data pipeline

The original data pack (PDFs + XLSX) lives at the repo root. Derived, machine-readable data lives in `data/`:

- `scripts/extract_data.py` — PDFs → `data/corpus/*.txt`, workbook → `data/structured/*.json`
- `scripts/compile_contracts.py` — LLM pass that compiles each signed agreement into the structured entitlements registry (`contract_terms.json`); checked in for deterministic runs, regenerable when contracts change
- Hand-curated registries derived from the docs: `doc_registry.json` (authority tiers), `policy_defaults.json` (current policy/SOP terms), `known_issues.json`

The dataset snapshot time (2026-08-16 11:00 IST, a **Sunday** — which matters for business-hours SLAs) is read from the workbook README sheet and used as "now" everywhere.

## Demo script

A suggested 5-minute walkthrough (matching the demo video) is in [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md); the scenario matrix with expected behaviors is in [docs/EVALUATION.md](docs/EVALUATION.md).
