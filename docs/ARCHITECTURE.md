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
│  fallback_agent.py / slm.py  no-API-key path: same tools, router+SLM   │
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

If `ANTHROPIC_API_KEY` isn't set, `run_agent_stream` hands the whole turn to a
different, narrower agent instead of erroring — see "Local fallback mode"
below.

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

## Local fallback mode (no API key)

Added so the app degrades gracefully instead of just erroring when there's no
Anthropic budget (e.g. a free-tier deploy) or the API is unreachable:
`agent.run_agent_stream` checks `config.HAS_ANTHROPIC_KEY` and, if unset,
delegates the whole turn to `app/fallback_agent.py`, which streams the exact
same SSE event shapes (`tool_start`/`tool_call`/`tool_result`/`text_delta`/
`pending_action`/`turn_done`) as the Claude loop — so the frontend needs zero
changes to render it, tool chips included.

**Split of responsibilities (deliberately the opposite of "a smaller agent
loop").** A ~135M-parameter model cannot reliably do Claude's job of
*choosing* tools across a multi-step plan — small models are weak at
function-calling, and this system's whole premise is not pretending to be
confident when it isn't. So:

1. A plain-Python keyword/entity router (`fallback_agent._route`) decides
   which *one* tool to call, from the same `tools.py` registry, dispatched
   through the same `tools.dispatch` — identical access control, identical
   rule engines, identical two-phase action confirmation. It resolves order
   /ticket/account IDs by regex, matches intent keywords (cancellation,
   credit, SLA, escalation, "what needs attention", etc.), and — mirroring
   the brief's own hypothetical-credit example — parses delay hours from
   either digits ("3 hours") or small spelled-out numbers ("three hours").
   Security-signal phrasing (API key exposure, "breach", "outage", ...)
   proactively prepares a P1 escalation, same as the full agent is instructed
   to do, still gated by the normal Confirm step.
2. The SLM (`app/slm.py`) only *phrases* the tool's already-computed result
   (rule-trace text, retrieved snippets) into a short reply. It is never
   handed raw policy text to reinterpret and never chooses actions, so a
   phrasing mistake can't invent a new policy outcome or an unconfirmed
   action.
3. Anything the router doesn't recognise, or that comes back empty from
   retrieval, prepares an escalation for the user to confirm rather than
   guessing — the same "don't know → ask a human" posture as the main prompt.

**Latency was the real design constraint, not model choice.** Measured
directly (see `scripts/fetch_slm_model.py`'s comment for the numbers) on a
Docker container capped at 512MB RAM / 0.1 CPU — Render's free-tier
instance type: SmolLM2-360M-Instruct generated at only ~0.16 tokens/sec;
SmolLM2-135M-Instruct (Q8_0) at ~0.5-0.6 tokens/sec with noticeably more
coherent output than the same model at Q4_K_M. Less expected: **prefill was
just as slow as decode** on a CPU this constrained (a ~160-token prompt took
up to 90 seconds just to process, before generating a single token) — the
usual assumption that prefill is "cheap" doesn't hold once a container is
throttled this hard. That finding drove three concrete decisions: swap to the
smaller 135M model despite lower raw quality, cap the facts handed to the
prompt tightly (`fallback_agent._MAX_FACTS`/`_MAX_FACT_CHARS`) since prompt
length now directly buys wall-clock time, and treat "generation" and
"prefill" as one combined time budget rather than two.
- **A hard, non-hanging deadline.** `slm.phrase_answer` passes a
  `stopping_criteria` callback to llama.cpp that checks a wall-clock deadline
  (`PARCELPILOT_SLM_TIMEOUT_S`, default 90s) after every token — including
  the first, so a slow prefill alone can trigger it. This stops generation
  cleanly (not a killed thread) and the result — even an empty completion —
  degrades to a deterministic bullet-list template of the same underlying
  facts, so the answer is always present and always correct, just sometimes
  less fluent than a phrased one. A `threading.Lock.acquire(timeout=...)`
  around the (single, shared) model instance gives the same bounded-wait
  guarantee if two requests land at once.
- **SSE keepalives, not a spinner hoping for the best.** Because a reply can
  legitimately take over a minute, `fallback_agent._phrase_with_keepalive`
  runs the blocking call via `asyncio.to_thread` and emits an SSE comment
  frame (`: keepalive`) every 8 seconds while it waits — verified to hold the
  connection open past two minutes end-to-end. The existing frontend already
  ignores non-`data:` SSE lines, so this needed no UI changes; the one UI
  change made was adding a line to the mode's notice message
  (`fallback_agent.FALLBACK_NOTICE`) that sets the expectation up front.
- **Why Render over Vercel for this path.** A resident model that loads once
  and stays warm needs a persistent process; Vercel's serverless functions
  would either reload the model on every cold start or need to fit the reload
  inside the request, both worse than a small always-running container. Local
  benchmarking also used real Docker `--memory`/`--cpus` limits rather than
  guessing, specifically because this is the one part of the system whose
  correctness depends on real resource constraints, not just logic.

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
- **Narrow, honest fallback vs a "smarter" one.** The no-API-key path could
  have tried harder to replicate full tool-calling with a bigger local model;
  instead it deliberately does less (single-tool routing, capped facts, a
  hard reply deadline) so that what it does do is still access-controlled,
  still cites real tool output, and never hangs or fabricates. On a genuinely
  free CPU tier, slow-and-correct beat fast-and-confident-but-occasionally-
  wrong as the failure mode to optimize for, consistent with this project's
  whole stance on trust.
