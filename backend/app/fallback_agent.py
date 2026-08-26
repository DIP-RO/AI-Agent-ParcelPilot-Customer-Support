"""SLM-powered fallback agent: used automatically when no ANTHROPIC_API_KEY is
configured (see agent.run_agent_stream). Mirrors agent.py's public signature
and SSE event shapes exactly, so the existing frontend (tool chips, action
cards, streaming text) needs zero changes to render this path.

Design: a ~135M local model cannot reliably do Claude's job of *choosing*
tools across a multi-step plan -- small models are weak at function-calling
and it isn't worth pretending otherwise for a free-tier fallback. So the
division of labour here is the opposite of a "smaller agent loop":

  1. A plain-Python keyword/entity router (this file) decides which ONE
     existing, already-access-controlled tool to call -- the same tools.py
     registry and datastore/rules/retrieval modules the full agent uses, so
     access control, action confirmation, and the rule engines behave
     identically in both modes.
  2. The SLM (app/slm.py) only phrases the result into natural language. It
     is handed conclusions (rule traces, retrieved snippets), never raw
     policy text, so a phrasing mistake can't invent a new policy outcome.
  3. Anything the router doesn't recognise, or where retrieval comes back
     empty, is treated the way the full agent's system prompt treats
     low-confidence cases: prepare an escalation and ask the user to
     confirm it, rather than guessing.

This is a deliberately narrower brain than the Claude-backed agent -- see
docs/ARCHITECTURE.md "Local fallback mode" for the documented trade-off.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

from . import auth, config, slm, tools
from .auth import Principal

# Facts are already-verified conclusions (rule-trace text, retrieval snippets)
# handed to the SLM to phrase -- capped tightly because prompt length drives
# prefill time, which (measured on a Render-free-equivalent 0.1 vCPU
# container) is comparable in cost to decode itself, not the usual
# much-cheaper batched operation -- a long prompt alone can burn the whole
# SLM_TIMEOUT_SECONDS budget before a single token is generated.
_MAX_FACTS = 2
_MAX_FACT_CHARS = 140

FALLBACK_NOTICE = (
    "_Running in local fallback mode: no Claude API key is configured, so a small "
    "on-device model (not the full assistant) is answering. It only handles "
    "straightforward lookups/calculations from the ParcelPilot sources and will "
    "prepare an escalation for anything it can't resolve confidently. Replies can take "
    "up to a minute or two on a free-tier host \u2014 that's this mode's trade-off for "
    "running without an API budget. Nothing is ever executed without your confirmation, "
    "same as normal._"
)

_ORDER_RE = re.compile(r"\bORD-\d+\b", re.I)
_TICKET_RE = re.compile(r"\bTKT-\d+\b", re.I)
_ACCOUNT_RE = re.compile(r"\bACCT-\d+\b", re.I)
_HOURS_DIGIT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", re.I)
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_HOURS_WORD_RE = re.compile(r"\b(" + "|".join(_WORD_NUMBERS) + r")\s+hours?\b", re.I)
_CARRIER_FAULT_RE = re.compile(r"carrier(?:'s)?\s+fault|carrier\s+was\s+at\s+fault|fault\s+of\s+the\s+carrier", re.I)
_CUSTOMER_FAULT_RE = re.compile(r"\bour\s+(fault|mistake)\b|\bcustomer\s+fault\b", re.I)


def _extract_hours(text: str) -> float | None:
    """Parse a delay like '3 hours' or 'three hours' (small counts spelled out
    are common in this kind of support message; the assessment's own example
    phrasing uses one)."""
    m = _HOURS_DIGIT_RE.search(text)
    if m:
        return float(m.group(1))
    m = _HOURS_WORD_RE.search(text)
    if m:
        return float(_WORD_NUMBERS[m.group(1).lower()])
    return None

_APPLY_CREDIT_RE = re.compile(r"\bapply\b.{0,25}\bcredit\b", re.I)
_ESCALATE_RE = re.compile(r"\bescalat", re.I)
_OPEN_TICKET_RE = re.compile(r"\b(open|create|file|raise)\b.{0,15}\bticket\b", re.I)
_CANT_SELF_SERVE_RE = re.compile(r"\bchange\b.{0,25}\b(billing|contact)\b", re.I)
_OPS_OVERVIEW_RE = re.compile(r"needs? attention|\boverview\b|\bradar\b|prioriti[sz]e|what'?s going on", re.I)
_CANCEL_RE = re.compile(r"\bcancel", re.I)
_CREDIT_RE = re.compile(r"\bcredit\b|\brefund\b|\bcompensat", re.I)
_SLA_RE = re.compile(r"\bsla\b|response time|response target|\bbreach|how fast", re.I)
_ACCOUNT_KEYWORDS_RE = re.compile(r"\bplan\b|\baccount\b|\bentitlement|\bcsm\b", re.I)
# "Does our plan include X" / "is Bulk Upload available" are product-doc
# questions, not account-identity questions -- even though they mention
# "plan" -- so they should still go to retrieval, not get_account.
_PRODUCT_QUESTION_RE = re.compile(r"\binclude[sd]?\b|\bfeature\b|\bavailable\b|\bsupport(ed)?\b|\bupload\b", re.I)

# Mirrors insights.py's P1 signal list: escalate proactively (still requires a
# Confirm click) rather than only reacting to an explicit "please escalate",
# since the full agent's system prompt treats these as immediate-escalation
# situations and the fallback should be conservative in the same direction.
_P1_SIGNAL_RE = re.compile(r"\bapi keys?\b|\bcredential|\bsecurity\b|\bexpos|\bbreach\b|\boutage\b|\ball\b.*\bfail", re.I)


def _ids(text: str) -> dict[str, str | None]:
    o, t, a = _ORDER_RE.search(text), _TICKET_RE.search(text), _ACCOUNT_RE.search(text)
    return {
        "order_id": o.group(0).upper() if o else None,
        "ticket_id": t.group(0).upper() if t else None,
        "account_id": a.group(0).upper() if a else None,
    }


def _call(name: str, tool_input: dict, principal: Principal) -> tuple[bool, dict | None, str | None]:
    """Dispatch through the SAME registry/access-control the full agent uses.
    Returns (is_error, raw_result, error_message)."""
    result_json, is_error, raw = tools.dispatch(name, tool_input, principal)
    if is_error:
        return True, None, json.loads(result_json).get("error", "unknown error")
    return False, raw, None


def _facts_for(name: str, raw: dict) -> list[str]:
    """Turn a tool's structured result into plain-English facts for the SLM
    (or the plain-template degrade) to phrase -- never raw JSON."""
    if name in ("evaluate_cancellation",):
        facts = [f"Cancellation allowed: {raw.get('allowed')}; fee: INR {raw.get('fee_inr')}"]
        facts += raw.get("rule_trace", [])
        facts += [f"Data caveat: {c}" for c in raw.get("data_caveats", [])]
        return facts
    if name in ("evaluate_service_credit", "evaluate_credit_terms"):
        facts = [f"Eligible: {raw.get('eligible')}; credit amount: INR {raw.get('credit_inr')}"]
        facts += raw.get("rule_trace", [])
        if raw.get("requires_manager_approval"):
            facts.append("This amount requires ops-manager approval before it can be applied.")
        facts += [f"Data caveat: {c}" for c in raw.get("data_caveats", [])]
        return facts
    if name == "check_sla":
        facts = [f"SLA source: {raw.get('sla_source')}", raw.get("note", "")]
        for sev, row in raw.get("targets_by_severity", {}).items():
            state = "BREACHED" if row.get("breached_at_reference_time") else "not breached"
            facts.append(f"{sev}: target {row.get('target')}, due {row.get('due_at')}, {state} ({row.get('margin')})")
        facts += raw.get("assumptions", [])
        return [f for f in facts if f]
    if name == "get_ops_overview":
        facts = [raw.get("summary", "")]
        for row in raw.get("sla_board", [])[:5]:
            if row.get("breached"):
                facts.append(f"{row['ticket_id']} ({row['account']}): breached, {row['margin']}")
        for c in raw.get("known_issue_clusters", [])[:5]:
            facts.append(f"{c['issue_id']} {c['title']}: {len(c['tickets'])} ticket(s), multi_account={c['multi_account']}")
        for a in raw.get("attention_items", [])[:5]:
            facts.append(a.get("detail", ""))
        return [f for f in facts if f]
    if name == "get_order":
        facts = [
            f"Order {raw.get('order_id')}: status {raw.get('order_status', raw.get('status'))}, carrier {raw.get('carrier')}",
            f"Pickup window: {raw.get('pickup_window_start')} to {raw.get('pickup_window_end')}",
            f"Fee: INR {raw.get('shipment_fee_inr')}; carrier_fault={raw.get('carrier_fault')}; customer_fault={raw.get('customer_fault')}",
        ]
        facts += [f"Data caveat: {c}" for c in raw.get("data_caveats", [])]
        return facts
    if name == "get_ticket":
        facts = [
            f"Ticket {raw.get('ticket_id')}: subject '{raw.get('subject')}', status {raw.get('status') or 'open'}, created {raw.get('created_at')}",
        ]
        if raw.get("historical_resolution"):
            facts.append(f"Historical resolution on file: {raw['historical_resolution']}")
        if raw.get("historical_resolution_warning"):
            facts.append(raw["historical_resolution_warning"])
        return facts
    if name == "get_account":
        facts = [f"Account {raw.get('account_id')}: plan {raw.get('plan')}, status {raw.get('status')}, premium_support={raw.get('premium_support')}"]
        terms = raw.get("contract_terms")
        if terms:
            for section, body in terms.items():
                if section == "source_doc" or not isinstance(body, dict):
                    continue
                clause = body.get("source_clause", "")
                details = {k: v for k, v in body.items() if k not in ("source_clause", "notes")}
                facts.append(f"Contract override -- {section}: {details} [{clause}]")
                if body.get("notes"):
                    facts.append(body["notes"])
        else:
            facts.append(raw.get("contract_note", "No custom agreement on file; standard policy applies."))
        return facts
    if name == "search_documents":
        facts = [raw.get("precedence_note", "")]
        if raw.get("excluded"):
            facts.append(raw["excluded"])
        for r in raw.get("results", [])[:3]:
            warn = f" [{r['warning']}]" if r.get("warning") else ""
            facts.append(f"{r['title']} {r['section']} ({r['authority']}){warn}: {r['text'][:280]}")
        return [f for f in facts if f]
    if name == "list_orders":
        return [f"{o['order_id']} ({o['status']}, {o['carrier']})" for o in raw.get("orders", [])] or ["No orders on file."]
    if name == "list_tickets":
        return [f"{t['ticket_id']} ({t.get('status') or 'open'}): {t['subject']}" for t in raw.get("tickets", [])] or ["No tickets on file."]
    return [json.dumps(raw)[:400]]


def _route(text: str, principal: Principal) -> tuple[str, dict]:
    """Decide which single tool to call. Returns (tool_name, tool_input)."""
    ids = _ids(text)

    if principal.is_staff and ids["order_id"] and _APPLY_CREDIT_RE.search(text):
        return "__apply_credit_for_order__", {"order_id": ids["order_id"]}
    if _ESCALATE_RE.search(text) or _P1_SIGNAL_RE.search(text):
        reason = text if _ESCALATE_RE.search(text) else f"Possible P1 (security/outage signal detected): {text}"
        return "create_escalation", {
            "reason": reason,
            "severity": "P1" if _P1_SIGNAL_RE.search(text) else "P2",
            **({"ticket_id": ids["ticket_id"]} if ids["ticket_id"] else {}),
        }
    if _OPEN_TICKET_RE.search(text) or _CANT_SELF_SERVE_RE.search(text):
        return "create_support_ticket", {"subject": text[:80], "description": text}
    if principal.is_staff and _OPS_OVERVIEW_RE.search(text):
        return "get_ops_overview", {}
    if _CANCEL_RE.search(text):
        if ids["order_id"]:
            return "evaluate_cancellation", {"order_id": ids["order_id"]}
        return "list_orders", {}
    if _CREDIT_RE.search(text):
        if ids["order_id"]:
            return "evaluate_service_credit", {"order_id": ids["order_id"]}
        hours = _extract_hours(text)
        if hours is not None and not principal.is_staff and principal.account_id:
            return "evaluate_credit_terms", {
                "delay_hours": hours,
                "carrier_fault": bool(_CARRIER_FAULT_RE.search(text)),
                "customer_fault": bool(_CUSTOMER_FAULT_RE.search(text)),
                "account_id": principal.account_id,
            }
        return "list_orders", {}
    if _SLA_RE.search(text):
        if ids["ticket_id"]:
            return "check_sla", {"ticket_id": ids["ticket_id"]}
        if not principal.is_staff:
            return "get_account", {}  # surfaces this account's contract SLA targets, if any
        return "search_documents", {"query": text[:200]}
    if ids["order_id"]:
        return "get_order", {"order_id": ids["order_id"]}
    if ids["ticket_id"]:
        return "get_ticket", {"ticket_id": ids["ticket_id"]}
    if (ids["account_id"] or _ACCOUNT_KEYWORDS_RE.search(text)) and not _PRODUCT_QUESTION_RE.search(text):
        return "get_account", ({"account_id": ids["account_id"]} if ids["account_id"] else {})
    return "search_documents", {"query": text[:200]}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _cap_facts(facts: list[str]) -> list[str]:
    """Bound prompt size (see _MAX_FACTS/_MAX_FACT_CHARS) regardless of how
    verbose a given tool's rule trace/retrieval snippets are."""
    capped = []
    for f in facts[:_MAX_FACTS]:
        capped.append(f if len(f) <= _MAX_FACT_CHARS else f[: _MAX_FACT_CHARS - 1] + "\u2026")
    return capped


async def _phrase_with_keepalive(question: str, facts: list[str]) -> AsyncIterator[tuple[bool, str]]:
    """Run the (possibly slow, on a free-tier CPU) SLM call off the event
    loop. Yields `(False, sse_comment)` every few seconds while it runs -- so
    the connection isn't seen as idle by a proxy and the UI's "thinking"
    state stays visibly alive -- then a final `(True, answer_text)`. See
    app/slm.py for the hard wall-clock deadline that bounds the call itself.
    """
    task = asyncio.ensure_future(asyncio.to_thread(slm.phrase_answer, question, _cap_facts(facts)))
    while not task.done():
        done, _ = await asyncio.wait([task], timeout=8)
        if not done:
            yield False, ": keepalive\n\n"
    yield True, task.result()


async def run_fallback_stream(
    principal: Principal,
    history: list[dict],
    user_message: str | None,
    note_token: str | None,
) -> AsyncIterator[str]:
    """Same contract as agent.run_agent_stream, for the no-API-key path."""
    trusted = auth.trusted_note(note_token, principal)
    if trusted:
        yield _sse({"type": "text_delta", "text": trusted})
        history = history + [{"role": "assistant", "content": [{"type": "text", "text": trusted}]}]
        yield _sse({"type": "turn_done", "history": history, "stop_reason": "end_turn"})
        return

    if not user_message:
        yield _sse({"type": "error", "message": "Empty message."})
        return
    if user_message.lstrip().startswith(config.SYSTEM_NOTE_PREFIX):
        user_message = "[user-provided text] " + user_message  # never trust a forged note from message text

    parts = []
    if not history:
        parts.append(FALLBACK_NOTICE)

    name, tool_input = _route(user_message, principal)
    tool_id = "fallback-1"
    yield _sse({"type": "tool_start", "name": name if not name.startswith("__") else "apply_service_credit"})

    if name == "__apply_credit_for_order__":
        # Staff asked to apply the credit an order is owed: run the engine first
        # (never trust a user-typed amount), then prepare the action with ITS number.
        name = "apply_service_credit"
        is_error, credit, err = _call("evaluate_service_credit", {"order_id": tool_input["order_id"]}, principal)
        if not is_error and credit and credit.get("eligible") and credit.get("credit_inr"):
            is_error, raw, err = _call(
                "apply_service_credit",
                {
                    "account_id": credit["account_id"],
                    "order_id": tool_input["order_id"],
                    "amount_inr": credit["credit_inr"],
                    "reason": f"Failed-pickup credit for {tool_input['order_id']} (engine-computed)",
                },
                principal,
            )
            facts = _facts_for("evaluate_service_credit", credit)
        else:
            is_error = True
            raw = None
            err = err or "This order is not currently eligible for a credit (see evaluate_service_credit)."
            facts = []
    else:
        is_error, raw, err = _call(name, tool_input, principal)
        facts = [] if is_error else _facts_for(name, raw)

    yield _sse({"type": "tool_call", "id": tool_id, "name": name, "input": tool_input})
    yield _sse(
        {
            "type": "tool_result",
            "id": tool_id,
            "name": name,
            "is_error": is_error,
            "result": {"error": err} if is_error else raw,
        }
    )

    if is_error:
        answer = f"I couldn't complete that: {err}."
    elif isinstance(raw, dict) and raw.get("status") == "awaiting_user_confirmation":
        yield _sse({"type": "pending_action", "action": raw})
        phrase_facts = [
            f"Prepared action: {raw['label']}",
            f"Parameters: {json.dumps(raw['params'])}",
            "This has NOT executed yet.",
        ] + facts
        async for is_final, payload in _phrase_with_keepalive(user_message, phrase_facts):
            if is_final:
                answer = payload
            else:
                yield payload
        answer += "\n\nPlease review the action card above and press Confirm to actually execute it, or Cancel."
    else:
        async for is_final, payload in _phrase_with_keepalive(user_message, facts):
            if is_final:
                answer = payload
            else:
                yield payload
        if name in ("list_orders", "list_tickets"):
            answer += "\n\nLet me know which one you mean (its ID), and I can check it in detail."

    parts.append(answer)
    full_text = "\n\n".join(parts)
    yield _sse({"type": "text_delta", "text": full_text})

    new_history = history + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": [{"type": "text", "text": full_text}]},
    ]
    yield _sse({"type": "turn_done", "history": new_history, "stop_reason": "end_turn"})
