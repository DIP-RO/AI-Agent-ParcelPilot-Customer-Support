# Product Note

## Which additional client problem, and how

I addressed **both**, with different depths:

**Problem 2 (Trust & Reliability) is built into the core**, because for a
support agent that can quote prices and promise credits, trust isn't a feature
— it's the product. Concretely: deterministic engines for every money/deadline
decision with clause-cited rule traces; authority-tiered retrieval with
deprecated-document quarantine; "may be incorrect" labels on historical
resolutions attached at the data layer; stale-status caveats driven by live
known issues; refusal to promise credits on missing fault data; and structural
(not prompt-based) confirmation before any action. The data pack is boobytrapped
— two poisoned historical answers, a deprecated policy, contract overrides, a
Sunday snapshot — and each trap has a specific mechanism plus a test against it.

**Problem 1 (Proactive Issue Detection) is the Ops Radar** — a staff dashboard
and an agent tool (`get_ops_overview`) computed from the same data: an SLA
board with likely breaches (TKT-501 and TKT-505 are already breached at the
snapshot; LumenWorks' business-hours clock correctly hasn't started on a
Sunday), tickets clustered by known product issue (KI-208 has two tickets from
the same account — a repeat complaint; KI-211 links a ticket to an order whose
status may be stale), unactioned entitlements (LumenWorks is owed a INR 300
credit for ORD-2002 that nobody applied), pending cancellation requests, and a
list of historical answers to re-verify. Every finding shows its evidence, and
"Discuss in chat" hands the item to the agent to investigate and act (with
confirmation).

## What I'd build next, in priority order

1. **Feedback + evaluation loop.** Thumbs up/down with reason capture on every
   answer, a golden-set of scenario questions run against every prompt/model
   change (the seeds are in docs/EVALUATION.md), and an escalation-outcome
   review queue. Without this you can't safely iterate on prompts — it's the
   highest-leverage next thing.
2. **Real ticketing/actions integration** (Zendesk/Freshdesk-style): durable
   action store with audit trail, idempotency keys, and a first-response
   timestamp field — which the SLA engine currently has to assume away.
3. **Draft-reply mode for the ops team**: the internal copilot drafts the
   customer-facing reply with citations; the human approves/edits/sends. This
   is the fastest path to value for a 20-person team handling hundreds of
   requests — automation rate can grow later as trust accumulates.
4. **Contract-terms pipeline**: when an agreement is signed/renewed, the
   compile step (scripts/compile_contracts.py) runs automatically, a human
   reviews the structured diff, and effective-date awareness switches terms on
   time. Also: aggregate credit tracking so the Northstar INR 5,000 monthly cap
   is checkable rather than caveated.
5. **Notification hooks for Ops Radar** (Slack/email digests on new breaches or
   cluster growth) so proactive detection doesn't require opening a dashboard.
6. **Retrieval upgrades when the corpus grows**: hybrid BM25+embeddings,
   effective-date filtering at query time, and per-chunk clause anchors.

## Intentionally left out

- Real identity (SSO/passwords) — mocked per the brief; scoping and signing are
  real.
- A database — the dataset is a fixed snapshot; mutations are an in-memory
  overlay, which also keeps the demo hosting free.
- Multi-turn server-side session storage, rate limiting, observability stack
  (would be OpenTelemetry + request logs of every tool call verdict).
- LLM-based severity classification in the Radar (heuristics are transparent
  and free; the chat agent does policy-grounded classification on demand).
- Voice/email channels, CSAT surveys, multilingual support.

## One metric

**Correct-resolution rate without human correction** — the share of assistant
answers/actions that a reviewer (or the escalation outcome) confirms as
correct and complete, sampled weekly against the golden set and live traffic.
Deflection or speed metrics reward confident wrongness; this metric is the
direct measure of the thing that determines adoption here: *can the team trust
what it says?* I'd pair it with escalation precision (of the things it chose to
escalate, how many truly needed a human) to catch over-escalation as the
failure mode of an over-cautious agent.
