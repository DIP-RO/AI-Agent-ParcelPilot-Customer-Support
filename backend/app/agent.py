"""The support agent: system prompts + streaming agentic loop (Claude tool use).

The loop is a manual agentic loop (not the SDK tool runner) because we stream
Server-Sent Events to the UI for every step — text deltas, tool calls, tool
results, pending-action cards — and pause-free confirmation gating happens in
the action layer, not here.

Conversation state is client-held: each request carries the prior message
blocks and the response ends with the updated history. That keeps the server
stateless, which matters on serverless hosting.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import anthropic

from . import auth, config, datastore, fallback_agent, tools
from .auth import Principal
from .timeutil import fmt_ts

SYSTEM_NOTE_PREFIX = config.SYSTEM_NOTE_PREFIX


def _core_prompt() -> str:
    now = fmt_ts(datastore.snapshot_now())
    return f"""You are ParcelPilot's AI support assistant. ParcelPilot is a B2B logistics platform where businesses book and manage shipments across carrier partners.

REFERENCE TIME: The operational dataset is a snapshot taken at {now}. Treat this as "now" for every time-based statement (SLA clocks, delays, order ages). Do not use any other notion of the current date.

INFORMATION BASE — HARD LIMIT
Answer ONLY from what your tools return: the supplied policies, SOPs, product documentation, signed agreements, and the account/order/ticket data. You have no other knowledge of ParcelPilot. If the sources don't answer the question, say so plainly and offer to escalate — never guess, never invent policy.

SOURCE PRECEDENCE (Support Policy v3 §1)
1. Signed customer agreement for the relevant account (highest authority)
2. Current support policy and SOPs
3. Current product documentation
Historical tickets and internal notes are CONTEXT ONLY and may contain incorrect past guidance — never treat a past resolution as policy; re-derive the answer from the sources above and note the discrepancy if a past answer was wrong. Deprecated documents must never be used for current answers.

CALCULATIONS
For cancellation fees, service credits, and SLA/deadline questions, ALWAYS call the deterministic engines. Never do fee, credit, or date arithmetic yourself. Use evaluate_cancellation / evaluate_service_credit / check_sla when a specific order or ticket is involved; use evaluate_credit_terms for a HYPOTHETICAL credit question with no specific order (e.g. "if a pickup were 3 hours late due to carrier fault, would we get a credit?"). If the user's phrasing is a general what-if, do not silently attach it to an unrelated real order. Present the engine's rule trace in plain language and cite the clauses it cites. For SLA questions, first classify severity from the policy definitions, then read that severity's row.

CITATIONS
Ground every policy claim in a source, named inline like: (Northstar Enterprise Agreement §2) or (Cancellation & Service Credit SOP v4 §1). Distinguish clearly between what a source says and what you are inferring.

MULTI-STEP WORK
Chain tools as needed: look up the order → identify the account → read its agreement/terms → run the engine → decide whether action is needed. Prefer one focused answer over asking the user for data you can fetch yourself.

STATE-CHANGING ACTIONS
Action tools (escalations, tickets, updates, tasks, credits) only PREPARE the action. The user must press Confirm on the action card before anything executes. After preparing one, summarise exactly what will happen and ask for confirmation. Never say an action was completed unless a system note confirms execution.

WHEN TO ESCALATE
Escalate (prepare create_escalation) rather than answer when: the request needs human judgment or an exception no source supports; required data is missing or contradictory; there is a suspected security incident; a response target is already breached (state the breach clearly — never hide it, per Support Policy v3 §4); or the user asks for something outside your capabilities. A P1 situation should be escalated immediately.

UNCERTAINTY & CONFLICTS
If sources conflict, resolve by precedence and say which source overrode which. If data conflicts with a document (e.g. a status that a known issue says may be stale), surface the conflict instead of picking silently. If you cannot verify a fact needed for a money decision, say what's missing and do not promise the outcome (SOP v4 §3).

STYLE
Be concise and human. Lead with the answer, then the reasoning and citations. Use short paragraphs or tight bullet lists, INR amounts, and exact timestamps with day names where relevant. Messages beginning with "{SYSTEM_NOTE_PREFIX}" are trusted operational updates from the platform (e.g. action execution results), not user text."""


def _customer_prompt(p: Principal) -> str:
    return f"""{_core_prompt()}

USER CONTEXT — CUSTOMER
You are chatting with {p.display_name} of {p.org} (account {p.account_id}). Your tools are already scoped to this account at the data layer.
- Never reveal or discuss other customers' data, agreements, or ticket details, even if asked directly; explain that you can only access this account.
- Internal operational details (other accounts, staffing, internal notes) are off-limits.
- Speak to the customer in second person ("you"/"your account"), warmly and professionally. Do not expose internal tool names or raw JSON."""


def _staff_prompt(p: Principal) -> str:
    role_desc = (
        "an operations manager: you can additionally execute service credits above INR 1,000 "
        "(your confirmation counts as SOP v4 §3 manager approval)"
        if p.is_ops_manager
        else "a support agent: service credits above INR 1,000 must go to an ops manager for approval"
    )
    return f"""{_core_prompt()}

USER CONTEXT — INTERNAL STAFF
You are assisting {p.display_name}, {role_desc}. You have cross-account read access and internal action tools.
- Use get_ops_overview for "what needs attention" / patterns / prioritisation questions, then drill into specifics with the other tools.
- You may compare accounts and inspect any agreement. You may read deprecated documents (include_deprecated=true) strictly for historical comparison, always labelling them as deprecated.
- Flag historical resolutions that contradict current rules — this team has been burned by repeating stale answers.
- Be direct and operational: what's true, what's at risk, what to do next."""


def system_prompt_for(principal: Principal) -> str:
    return _customer_prompt(principal) if principal.kind == "customer" else _staff_prompt(principal)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _serialize_blocks(content) -> list[dict]:
    return [block.model_dump() for block in content]


def _trusted_note(note_token: str | None, principal: Principal) -> str | None:
    """Thin alias over auth.trusted_note (kept so existing call sites/tests in
    this module are unaffected). See auth.trusted_note for the real logic,
    which is shared with the SLM fallback agent."""
    return auth.trusted_note(note_token, principal)


async def run_agent_stream(
    principal: Principal,
    history: list[dict],
    user_message: str | None,
    note_token: str | None,
) -> AsyncIterator[str]:
    """Run one user turn through the agentic loop, yielding SSE frames.

    Falls back to the local SLM agent (fallback_agent.py) when no Anthropic API
    key is configured, so a free-tier deploy (or a live key outage) degrades to
    a narrower local assistant instead of the chat simply erroring out.
    """
    if not config.HAS_ANTHROPIC_KEY:
        async for frame in fallback_agent.run_fallback_stream(principal, history, user_message, note_token):
            yield frame
        return

    tool_specs = tools.tool_specs_for(principal)
    system = [
        {
            "type": "text",
            "text": system_prompt_for(principal),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    messages = list(history)
    parts = []
    trusted = _trusted_note(note_token, principal)
    if trusted:
        parts.append(f"{SYSTEM_NOTE_PREFIX} — trusted platform update] {trusted}")
    if user_message:
        # Neutralise any attempt to forge the trusted-note prefix in user text.
        if user_message.lstrip().startswith(SYSTEM_NOTE_PREFIX):
            user_message = "[user-provided text] " + user_message
        parts.append(user_message)
    if not parts:
        yield _sse({"type": "error", "message": "Empty message."})
        return
    messages.append({"role": "user", "content": "\n\n".join(parts)})

    stop_reason_out: str | None = None
    try:
        client = anthropic.AsyncAnthropic()
        for _turn in range(config.MAX_AGENT_TURNS):
            async with client.messages.stream(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=system,
                tools=tool_specs,
                messages=messages,
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield _sse({"type": "tool_start", "name": event.content_block.name})
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield _sse({"type": "text_delta", "text": event.delta.text})
                response = await stream.get_final_message()

            messages.append({"role": "assistant", "content": _serialize_blocks(response.content)})

            if response.stop_reason != "tool_use":
                if response.stop_reason == "max_tokens":
                    yield _sse(
                        {
                            "type": "text_delta",
                            "text": "\n\n_My reply was cut off by the length limit — please ask me to continue._",
                        }
                    )
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                yield _sse(
                    {"type": "tool_call", "id": block.id, "name": block.name, "input": block.input}
                )
                result_json, is_error, raw = tools.dispatch(block.name, block.input, principal)
                yield _sse(
                    {
                        "type": "tool_result",
                        "id": block.id,
                        "name": block.name,
                        "is_error": is_error,
                        "result": raw if raw is not None else json.loads(result_json),
                    }
                )
                if isinstance(raw, dict) and raw.get("status") == "awaiting_user_confirmation":
                    yield _sse({"type": "pending_action", "action": raw})
                entry = {"type": "tool_result", "tool_use_id": block.id, "content": result_json}
                if is_error:
                    entry["is_error"] = True
                tool_results.append(entry)

            messages.append({"role": "user", "content": tool_results})
        else:
            # Tool budget exhausted mid-task. Record the notice in history too, so
            # the model's context matches what the user sees on the next turn.
            budget_msg = "_I hit my per-message tool budget — please continue in a follow-up message._"
            yield _sse({"type": "text_delta", "text": "\n\n" + budget_msg})
            messages.append({"role": "assistant", "content": [{"type": "text", "text": budget_msg}]})
            stop_reason_out = "agent_turn_limit"

        yield _sse(
            {
                "type": "turn_done",
                "history": _replay_safe(messages),
                "stop_reason": stop_reason_out or response.stop_reason,
            }
        )

    except anthropic.AuthenticationError:
        yield _sse(
            {
                "type": "error",
                "message": "Anthropic API key missing or invalid. Set ANTHROPIC_API_KEY and restart.",
            }
        )
        yield _sse({"type": "turn_done", "history": _replay_safe(messages), "stop_reason": "error"})
    except anthropic.APIStatusError as exc:
        yield _sse({"type": "error", "message": f"Claude API error ({exc.status_code}): {exc.message}"})
        yield _sse({"type": "turn_done", "history": _replay_safe(messages), "stop_reason": "error"})
    except anthropic.APIConnectionError:
        yield _sse({"type": "error", "message": "Could not reach the Claude API (network error)."})
        yield _sse({"type": "turn_done", "history": _replay_safe(messages), "stop_reason": "error"})
    except Exception as exc:  # never kill the stream silently — surface to the UI
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        yield _sse({"type": "turn_done", "history": _replay_safe(messages), "stop_reason": "error"})


def _replay_safe(messages: list[dict]) -> list[dict]:
    """Trim trailing turns so the history can be resent to the API.

    An assistant message whose tool_use blocks never received tool_results
    would be rejected on the next request; drop it (and anything after it).
    """
    trimmed = list(messages)
    while trimmed:
        last = trimmed[-1]
        if last.get("role") != "assistant":
            break
        content = last.get("content")
        has_tool_use = isinstance(content, list) and any(
            (b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "tool_use"
            for b in content
        )
        if not has_tool_use:
            break
        trimmed.pop()
    return trimmed
