"""Agent tool registry: schemas, role-based availability, scoped dispatch.

Three classes of tools (assessment requirement #3):
  - document search/retrieval  (search_documents, read_document)
  - structured lookup/calculation (accounts/orders/tickets + rules engines)
  - state-changing actions (prepare-only; execution needs UI confirmation)

The registry filters tools by principal, and every handler receives the
principal so scoping happens in the data layer. The model never sees a tool it
isn't allowed to call, and calling one out of scope still fails server-side.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import actions, datastore, insights, retrieval, rules
from .auth import Principal
from .corpus import read_document as corpus_read_document


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Handlers (principal-aware)

def _search_documents(p: Principal, inp: dict) -> Any:
    include_deprecated = bool(inp.get("include_deprecated")) and p.is_staff
    return retrieval.search(
        inp["query"], p.account_scope, include_deprecated=include_deprecated
    )


def _read_document(p: Principal, inp: dict) -> Any:
    include_deprecated = bool(inp.get("include_deprecated")) and p.is_staff
    return corpus_read_document(inp["doc_id"], p.account_scope, allow_deprecated=include_deprecated)


def _get_account(p: Principal, inp: dict) -> Any:
    account_id = inp.get("account_id") or p.account_id
    if not account_id:
        return {"accounts": datastore.list_accounts(p.account_scope)}
    acct = datastore.get_account(account_id, p.account_scope)
    terms = datastore.contract_terms(account_id)
    if terms:
        acct["contract_terms"] = terms
    else:
        acct["contract_terms"] = None
        acct["contract_note"] = "No custom agreement in the data pack; default policies apply."
    return acct


def _list_orders(p: Principal, inp: dict) -> Any:
    return {"orders": datastore.list_orders(p.account_scope, inp.get("account_id"))}


def _get_order(p: Principal, inp: dict) -> Any:
    return datastore.get_order(inp["order_id"], p.account_scope)


def _list_tickets(p: Principal, inp: dict) -> Any:
    return {"tickets": datastore.list_tickets(p.account_scope, inp.get("account_id"), inp.get("status"))}


def _get_ticket(p: Principal, inp: dict) -> Any:
    return datastore.get_ticket(inp["ticket_id"], p.account_scope)


def _evaluate_cancellation(p: Principal, inp: dict) -> Any:
    return rules.evaluate_cancellation(inp["order_id"], p.account_scope)


def _evaluate_service_credit(p: Principal, inp: dict) -> Any:
    return rules.evaluate_service_credit(inp["order_id"], p.account_scope)


def _evaluate_credit_terms(p: Principal, inp: dict) -> Any:
    account_id = inp.get("account_id") or p.account_id
    if not account_id:
        raise KeyError("account_id is required (staff must specify which account)")
    return rules.evaluate_credit_terms(
        account_id,
        float(inp["delay_hours"]),
        bool(inp["carrier_fault"]),
        bool(inp.get("customer_fault", False)),
        p.account_scope,
        shipment_fee_inr=inp.get("shipment_fee_inr"),
    )


def _check_sla(p: Principal, inp: dict) -> Any:
    return rules.check_sla(inp["ticket_id"], p.account_scope)


def _get_ops_overview(p: Principal, inp: dict) -> Any:
    return insights.ops_overview()


def _action_handler(action_type: str) -> Callable[[Principal, dict], Any]:
    def handler(p: Principal, inp: dict) -> Any:
        pending = actions.prepare(action_type, inp, p)
        return {
            **pending,
            "instruction_to_agent": (
                "This action is PREPARED but NOT executed. Tell the user exactly "
                "what will happen and ask them to press Confirm on the action card. "
                "Never claim the action has been performed."
            ),
        }
    return handler


# ---------------------------------------------------------------------------
# Registry

_COMMON_TOOLS: list[dict] = [
    {
        "name": "search_documents",
        "description": (
            "Keyword search across ParcelPilot policies, SOPs, product documentation "
            "and (only your own account's) signed agreements. Results include "
            "authority tier, status and effective dates — apply source precedence: "
            "signed agreement > current policy/SOP > product docs. Deprecated "
            "documents are excluded."
        ),
        "input_schema": _obj(
            {
                "query": {"type": "string", "description": "Search keywords, e.g. 'cancellation fee BOOKED'"},
                "include_deprecated": {
                    "type": "boolean",
                    "description": "STAFF ONLY: include deprecated documents, clearly labeled, for historical comparison.",
                },
            },
            ["query"],
        ),
        "handler": _search_documents,
    },
    {
        "name": "read_document",
        "description": (
            "Read one full document by doc_id (ids appear in search results and account "
            "records): support-policy-v3, cancellation-sop-v4, product-ops-guide, or a "
            "signed agreement you are entitled to see."
        ),
        "input_schema": _obj(
            {
                "doc_id": {"type": "string"},
                "include_deprecated": {"type": "boolean", "description": "STAFF ONLY: allow reading a deprecated document."},
            },
            ["doc_id"],
        ),
        "handler": _read_document,
    },
    {
        "name": "get_account",
        "description": (
            "Fetch an account's profile: plan, status, CSM, premium support, and the "
            "structured contract terms compiled from its signed agreement (SLA, "
            "cancellation and credit overrides with clause citations). Customers get "
            "their own account automatically."
        ),
        "input_schema": _obj(
            {"account_id": {"type": "string", "description": "e.g. ACCT-001. Omit for your own account (customers) or to list all accounts (staff)."}}
        ),
        "handler": _get_account,
    },
    {
        "name": "list_orders",
        "description": "List orders (optionally for one account). Customers see only their own orders.",
        "input_schema": _obj({"account_id": {"type": "string"}}),
        "handler": _list_orders,
    },
    {
        "name": "get_order",
        "description": (
            "Fetch one order: status, booking/pickup times, fee, fault flags, "
            "cancellation request, plus data_caveats when a current known issue means "
            "the recorded status may lag reality."
        ),
        "input_schema": _obj({"order_id": {"type": "string", "description": "e.g. ORD-1001"}}, ["order_id"]),
        "handler": _get_order,
    },
    {
        "name": "list_tickets",
        "description": "List support tickets (optionally by account and/or status). Customers see only their own tickets.",
        "input_schema": _obj({"account_id": {"type": "string"}, "status": {"type": "string", "description": "e.g. open, closed"}}),
        "handler": _list_tickets,
    },
    {
        "name": "get_ticket",
        "description": (
            "Fetch one support ticket. Historical resolutions on closed tickets are "
            "context only and may be incorrect — re-verify before repeating them."
        ),
        "input_schema": _obj({"ticket_id": {"type": "string", "description": "e.g. TKT-501"}}, ["ticket_id"]),
        "handler": _get_ticket,
    },
    {
        "name": "evaluate_cancellation",
        "description": (
            "Deterministic policy engine: can this order be cancelled, and at what fee? "
            "Applies the current SOP plus any signed-agreement override for the order's "
            "account, and returns the full rule trace with clause citations. ALWAYS use "
            "this instead of computing fees yourself."
        ),
        "input_schema": _obj({"order_id": {"type": "string"}}, ["order_id"]),
        "handler": _evaluate_cancellation,
    },
    {
        "name": "evaluate_service_credit",
        "description": (
            "Deterministic policy engine: is this order eligible for a failed-pickup "
            "service credit, and how much? Applies contract-specific thresholds/amounts "
            "where they exist, flags manager-approval and aggregate-cap requirements, "
            "and refuses to promise credits when fault data is missing. ALWAYS use this "
            "instead of computing credits yourself."
        ),
        "input_schema": _obj({"order_id": {"type": "string"}}, ["order_id"]),
        "handler": _evaluate_service_credit,
    },
    {
        "name": "evaluate_credit_terms",
        "description": (
            "Deterministic policy engine for a HYPOTHETICAL failed-pickup credit when "
            "there is no specific order to look up (e.g. 'if a pickup were 3 hours late "
            "due to carrier fault, would we get a credit?'). Applies the account's "
            "contract threshold/amount or the default SOP formula and returns a "
            "cited rule trace. Use this for what-if questions; use "
            "evaluate_service_credit when a concrete order_id is involved. ALWAYS use "
            "one of these instead of judging eligibility yourself."
        ),
        "input_schema": _obj(
            {
                "delay_hours": {"type": "number", "description": "Hours the pickup is past the scheduled window end."},
                "carrier_fault": {"type": "boolean"},
                "customer_fault": {"type": "boolean"},
                "shipment_fee_inr": {"type": "number", "description": "Optional; needed only to compute the default percentage-based amount."},
                "account_id": {"type": "string", "description": "Staff only; customers are scoped to their own account automatically."},
            },
            ["delay_hours", "carrier_fault"],
        ),
        "handler": _evaluate_credit_terms,
    },
    {
        "name": "check_sla",
        "description": (
            "Deterministic SLA calculator for a ticket: first-response targets for "
            "P1/P2/P3 from the account's contract (or plan defaults), due times using "
            "business-hours vs 24x7 clocks, and whether each target is breached at the "
            "reference time. You still classify the ticket's severity from the policy "
            "definitions, then read that row. ALWAYS use this instead of doing date "
            "math yourself."
        ),
        "input_schema": _obj({"ticket_id": {"type": "string"}}, ["ticket_id"]),
        "handler": _check_sla,
    },
    {
        "name": "create_escalation",
        "description": (
            "Prepare an escalation to the human support team (state-changing: requires "
            "user confirmation before it executes). Use when a request needs human "
            "judgment, an unsupported exception, breached SLAs, security incidents, or "
            "anything you cannot resolve confidently from the sources."
        ),
        "input_schema": _obj(
            {
                "ticket_id": {"type": "string", "description": "Existing ticket to escalate, if any."},
                "reason": {"type": "string", "description": "Why this needs a human, with the key facts."},
                "severity": {"type": "string", "enum": ["P1", "P2", "P3"], "description": "Your severity classification."},
            },
            ["reason"],
        ),
        "handler": _action_handler("create_escalation"),
    },
    {
        "name": "create_support_ticket",
        "description": (
            "Prepare a new support ticket (state-changing: requires user confirmation "
            "before it executes)."
        ),
        "input_schema": _obj(
            {
                "subject": {"type": "string"},
                "description": {"type": "string"},
            },
            ["subject", "description"],
        ),
        "handler": _action_handler("create_support_ticket"),
    },
]

_STAFF_TOOLS: list[dict] = [
    {
        "name": "update_ticket",
        "description": (
            "Prepare a ticket update — status change, reassignment and/or an internal "
            "note (state-changing: requires user confirmation before it executes)."
        ),
        "input_schema": _obj(
            {
                "ticket_id": {"type": "string"},
                "status": {"type": "string", "description": "e.g. open, pending_customer, closed"},
                "assigned_to": {"type": "string"},
                "note": {"type": "string", "description": "Internal note to append."},
            },
            ["ticket_id"],
        ),
        "handler": _action_handler("update_ticket"),
    },
    {
        "name": "create_followup_task",
        "description": (
            "Prepare an internal follow-up task (state-changing: requires user "
            "confirmation before it executes)."
        ),
        "input_schema": _obj(
            {
                "title": {"type": "string"},
                "detail": {"type": "string"},
                "related_ticket_id": {"type": "string"},
            },
            ["title"],
        ),
        "handler": _action_handler("create_followup_task"),
    },
    {
        "name": "apply_service_credit",
        "description": (
            "Prepare a service credit application (state-changing: requires user "
            "confirmation). Amounts above INR 1,000 can only be executed by an ops "
            "manager (SOP v4 §3). Run evaluate_service_credit first and use its amount."
        ),
        "input_schema": _obj(
            {
                "account_id": {"type": "string"},
                "order_id": {"type": "string"},
                "amount_inr": {"type": "number"},
                "reason": {"type": "string"},
            },
            ["account_id", "amount_inr", "reason"],
        ),
        "handler": _action_handler("apply_service_credit"),
    },
    {
        "name": "get_ops_overview",
        "description": (
            "Proactive issue radar across ALL support activity: SLA board with likely "
            "breaches, tickets clustered by known product issues (including "
            "multi-account impact), unactioned credits/cancellations, stale-status "
            "warnings, and historical answers that contradict current rules. Use for "
            "'what needs attention?' style questions."
        ),
        "input_schema": _obj({}),
        "handler": _get_ops_overview,
    },
]


def tools_for(principal: Principal) -> list[dict]:
    tools = list(_COMMON_TOOLS)
    if principal.is_staff:
        tools = tools + _STAFF_TOOLS
    return tools


def tool_specs_for(principal: Principal) -> list[dict]:
    """Anthropic API tool definitions (without handlers)."""
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in tools_for(principal)
    ]


def dispatch(name: str, tool_input: dict, principal: Principal) -> tuple[str, bool, Any]:
    """Execute a tool. Returns (result_json, is_error, raw_result)."""
    handler = next((t["handler"] for t in tools_for(principal) if t["name"] == name), None)
    if handler is None:
        return (
            json.dumps({"error": f"Tool {name!r} is not available to this user."}),
            True,
            None,
        )
    try:
        result = handler(principal, tool_input or {})
        return json.dumps(result, default=str), False, result
    except (datastore.AccessDenied, actions.ActionError) as exc:
        return json.dumps({"error": str(exc)}), True, None
    except KeyError as exc:
        return json.dumps({"error": f"Not found: {exc}"}), True, None
    except Exception as exc:  # surface unexpected failures to the model, not the user
        return json.dumps({"error": f"Tool failure: {type(exc).__name__}: {exc}"}), True, None
