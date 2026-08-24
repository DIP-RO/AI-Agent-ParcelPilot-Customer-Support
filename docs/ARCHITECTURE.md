# Architecture Note

```
┌────────────────────────── React SPA (Vite/TS) ──────────────────────────┐
│  Persona login · Chat (streams text + tool activity) · Action cards     │
│  with Confirm/Cancel · Ops Radar dashboard (staff)                      │
└──────────────┬──────────────────────────────────────────────────────────┘
               │ SSE (chat) + JSON (login, confirm, insights)
┌──────────────▼──────────────── FastAPI ─────────────────────────────────┐
│  auth.py      HMAC-signed persona tokens → Principal (scope, role)      │
│  agent.py     manual Claude tool-use loop, streamed as SSE events       │
│  tools.py     role-filtered registry; every handler takes the Principal │
│  actions.py   two-phase actions: prepare (signed) → confirm endpoint    │
│  insights.py  Ops Radar analytics (deterministic, evidence-listing)     │
│  rules.py     cancellation / credit / SLA engines with rule traces      │
│  retrieval.py BM25 over chunked docs, authority-aware, scope-filtered   │
│  datastore.py all structured reads, account scoping enforced here       │
└──────────────┬──────────────────────────────────────────────────────────┘
               │
   data/corpus/*.txt (from PDFs)      data/structured/*.json (workbook +
   with doc_registry authority tiers  compiled contract/policy registries)
```

## Agent design

A **manual agentic loop** over the Claude Messages API (streaming): the model
plans, calls tools, reads results, repeats until it answers (capped at 12
turns). A manual loop (rather than the SDK's tool runner) was chosen because
every step is re-emitted to the browser as SSE events — text deltas, tool
calls/results, pending-action cards — which is how the UI shows "which tool is
being used".

Two system prompts share a core (reference time, source precedence, "engines do
the math", citation discipline, escalation criteria) and differ by context:
customer (second-person, privacy rules, no internal detail) vs staff (cross-
account powers, deprecated-doc access, manager-approval rules). The server is
**stateless**: message history rides with each request and is returned updated
after each turn — required for serverless hosting, and it makes replay trivial.

Sonnet 5 by default (cost-effective for a support workload); the model is an
env var, nothing else changes.

## Tool design

Three required classes, thirteen common tools + four staff-only:

- **Retrieval**: `search_documents` (BM25 + authority metadata), `read_document`.
- **Structured lookup / calculation**: account/order/ticket getters, plus four
  *deterministic engines* — `evaluate_cancellation`, `evaluate_service_credit`,
  `check_sla`, and `evaluate_credit_terms` (the last for hypothetical "what if a
  pickup were N hours late" questions that name no specific order, so those
  money decisions stay in code too). The prompt instructs the model to never do
  fee/credit/date arithmetic itself; the engines return verdicts **with rule
  traces and clause citations** the model can quote. LLMs are unreliable at
  business-calendar arithmetic; codifying it also makes answers reproducible and
  testable.
- **State-changing (mocked)**: escalations, tickets, updates, tasks, credits —
  all two-phase (below).
- **Staff-only**: `get_ops_overview` (Ops Radar as a tool), `update_ticket`,
  `create_followup_task`, `apply_service_credit`.

Customers don't just get refusals for staff tools — the tools are absent from
their registry, and dispatch fails closed if called anyway.

## Access control and privacy

Enforced in the **data/tool layer**, per the brief:

- Login is mocked (pick a persona) but issues an HMAC-signed token; tampering
  is rejected.
- The token resolves to a `Principal` whose `account_scope` is applied inside
  `datastore`/`corpus` on every read. A customer's tool calls *cannot* return
  another account's rows or agreement — the model never receives the data, so
  no prompt injection can leak it. Prompt-level rules exist too, but only as a
  second layer.
- Role split: support agents read everything and act; only ops managers can
  execute credits > INR 1,000 (SOP v4 §3 manager approval), enforced in
  `actions.prepare`.

## Confirmation before actions (structural, not behavioral)

Action tools only **prepare**: they validate authorization + scope, then return
an HMAC-signed pending-action payload. The UI renders it as a card with
Confirm/Cancel. Only the separate `/api/actions/confirm` endpoint — driven by a
human click, verifying both the signature and that the confirmer is the same
principal — executes. The model literally has no tool that executes an action,
so "the agent must ask first" is an architectural invariant rather than a
prompt hope. Signing keeps this stateless across serverless instances.

## Document & structured-data handling

- PDFs are extracted once (`scripts/extract_data.py`) into a text corpus;
  chunking is per numbered section so citations map to clauses.
- Every document carries registry metadata: authority tier (1 = signed
  agreement, 2 = current policy/SOP, 3 = product docs, 9 = deprecated), status,
  effective dates, and account binding for agreements.
- Contract terms are **compiled** into a structured entitlements registry
  (`contract_terms.json`) — by `scripts/compile_contracts.py`, an LLM pass with
  a strict JSON schema over each agreement, checked in and reviewable. The
  engines consume the registry; the agent still retrieves and cites the
  agreement text. This mirrors how real platforms handle entitlements: legal
  text is the source of truth, a reviewed structured representation is the
  runtime.
- The workbook is exported to JSON per sheet; the snapshot time comes from its
  README sheet and is the reference "now" everywhere (it's a Sunday — the
  business-hours SLA cases hinge on that).

## Source reliability & conflict handling

- **Precedence** (from Support Policy v3 §1) is implemented three times over:
  in retrieval ranking + result metadata, in the engines (contract overrides
  defaults, with the override cited in the trace), and in the system prompt.
- **Deprecated policy v2** is quarantined: excluded from retrieval and reads by
  default; staff can opt in and results carry a loud DEPRECATED warning;
  customers can't opt in at all (the flag is dropped in the tool layer).
- **Historical ticket resolutions** are served with an explicit "context only,
  may be incorrect" warning attached at the data layer. The pack's two poisoned
  resolutions (TKT-450's wrong fee for Northstar, TKT-451's wrong 3,000-row
  claim) are the tests for this: the engines/current docs produce the right
  answer and the agent is prompted to flag the contradiction. Ops Radar lists
  all historical answers for re-verification.
- **Data vs document conflicts**: order records gain `data_caveats` when a live
  known issue undermines them (a BOOKED SwiftShip order whose pickup window has
  started may already be collected — KI-211), so the agent hedges before
  asserting "not picked up".
- **Uncertainty**: the credit engine refuses to promise credits when fault
  flags are missing (SOP v4 §3), and SLA verdicts state their assumptions (no
  first-response timestamps in the data; Mon–Fri 09:00–18:00 IST business
  hours).

## Major trade-offs

- **Curated registries vs pure retrieval-time reasoning.** Policy/contract
  terms are compiled to config (reviewable, deterministic, testable) instead of
  having the LLM re-interpret legal text per query. Cost: a new contract needs
  a compile step (scripted) — the right trade for money-affecting decisions.
- **BM25 vs embeddings.** Six short documents don't need a vector DB; BM25 is
  transparent, dependency-free, serverless-friendly, and the agent can
  reformulate queries. Revisit at hundreds of documents.
- **Client-held history & signed pending actions vs server sessions.** Chosen
  for statelessness on Vercel; cost is payload size on long chats.
- **In-memory action log / ticket overlay.** Actions are mocked per the brief;
  a real system needs a DB with an audit trail. Per-instance state on
  serverless is the accepted (documented) demo limitation.
- **Heuristic severity suggestions in Ops Radar** (transparent keyword rules,
  labeled as suggestions) instead of LLM classification per load: instant,
  free, explainable; the chat agent does the careful policy-based
  classification when it matters.
