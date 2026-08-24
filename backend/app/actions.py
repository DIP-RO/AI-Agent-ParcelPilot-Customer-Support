"""State-changing actions with a structural two-phase confirmation gate.

Phase 1 (agent): an action tool call only *prepares* the action. The payload is
HMAC-signed and returned to the UI as a pending-action card. The model cannot
execute anything.

Phase 2 (human): the UI's Confirm button POSTs the signed payload to
/api/actions/confirm, where the signature and the principal are re-verified and
the action executes. Confirmation is therefore enforced by the transport
architecture, not by prompt instructions.

Signed payloads keep the flow stateless (safe on serverless); the executed-
action log is in-memory per instance, which is fine for mocked actions and is
called out in the architecture note.
"""

from __future__ import annotations

import itertools
import threading

from . import auth, datastore
from .auth import Principal
from .timeutil import fmt_ts


class ActionError(Exception):
    pass


_log_lock = threading.Lock()
_action_log: list[dict] = []
_counter = itertools.count(1)

ACTION_TYPES = {
    "create_escalation": "Escalate to the ParcelPilot support team",
    "create_support_ticket": "Open a new support ticket",
    "update_ticket": "Update a support ticket",
    "create_followup_task": "Create an internal follow-up task",
    "apply_service_credit": "Apply a service credit to an account",
}

# Tool-layer authorisation: which personas may prepare which actions.
_CUSTOMER_ACTIONS = {"create_escalation", "create_support_ticket"}
_STAFF_ACTIONS = set(ACTION_TYPES)

MANAGER_APPROVAL_THRESHOLD_INR = 1000  # SOP v4 §3


def prepare(action_type: str, params: dict, principal: Principal) -> dict:
    """Phase 1: validate + sign a pending action. Never executes."""
    if action_type not in ACTION_TYPES:
        raise ActionError(f"Unknown action type {action_type!r}")
    allowed = _STAFF_ACTIONS if principal.is_staff else _CUSTOMER_ACTIONS
    if action_type not in allowed:
        raise ActionError(
            f"Access denied: persona '{principal.display_name}' is not authorised "
            f"to perform '{action_type}'. Customers may create escalations or "
            "support tickets; other actions are staff-only."
        )

    params = dict(params)
    approval_note = None
    if action_type == "apply_service_credit":
        amount = float(params.get("amount_inr", 0))
        if amount <= 0:
            raise ActionError("apply_service_credit requires a positive amount_inr")
        if amount > MANAGER_APPROVAL_THRESHOLD_INR and not principal.is_ops_manager:
            raise ActionError(
                f"Credits above INR {MANAGER_APPROVAL_THRESHOLD_INR} require manager "
                "approval (Cancellation & Service Credit SOP v4 §3). Prepare an "
                "escalation to an ops manager instead, or have an ops manager persona "
                "apply it."
            )
        if amount > MANAGER_APPROVAL_THRESHOLD_INR:
            approval_note = (
                f"Amount exceeds INR {MANAGER_APPROVAL_THRESHOLD_INR}; executing as "
                "ops manager counts as the SOP v4 §3 manager approval."
            )

    # Scope check: an action referencing a ticket/account must be in scope.
    ticket_id = params.get("ticket_id") or params.get("related_ticket_id")
    if ticket_id:
        ticket = datastore.get_ticket(ticket_id, principal.account_scope)  # raises if out of scope
        params.setdefault("account_id", ticket["account_id"])
    account_id = params.get("account_id")
    if account_id and principal.account_scope and account_id != principal.account_scope:
        raise ActionError("Access denied: action references a different account.")
    if not account_id and principal.account_id:
        params["account_id"] = principal.account_id

    payload = {
        "t": "pending_action",
        "action_type": action_type,
        "params": params,
        "persona_id": principal.persona_id,
    }
    pending = {
        "action_type": action_type,
        "label": ACTION_TYPES[action_type],
        "params": params,
        "requested_by": principal.display_name,
        "signed_payload": auth.sign_blob(payload),
        "status": "awaiting_user_confirmation",
    }
    if approval_note:
        pending["approval_note"] = approval_note
    return pending


def confirm(signed_payload: str, principal: Principal) -> dict:
    """Phase 2: verify the signature and the confirming user, then execute."""
    data = auth.verify_blob(signed_payload)
    if data.get("t") != "pending_action":
        raise ActionError("Not a pending action payload")
    if data["persona_id"] != principal.persona_id:
        raise ActionError("Access denied: this pending action belongs to a different user.")
    return _execute(data["action_type"], data["params"], principal)


def _execute(action_type: str, params: dict, principal: Principal) -> dict:
    with _log_lock:
        seq = next(_counter)
    now = fmt_ts(datastore.snapshot_now())
    record = {
        "action_type": action_type,
        "params": params,
        "executed_by": principal.display_name,
        "persona_id": principal.persona_id,
        "executed_at": now,
        "status": "completed",
    }

    if action_type == "create_escalation":
        record["record_id"] = f"ESC-{1000 + seq}"
        record["summary"] = (
            f"Escalation {record['record_id']} created"
            + (f" for {params['ticket_id']}" if params.get("ticket_id") else "")
            + f": {params.get('reason', params.get('summary', ''))}"
        )
        if params.get("ticket_id"):
            datastore.update_ticket(
                params["ticket_id"],
                {"escalated": True},
                note=f"Escalation {record['record_id']} created by {principal.display_name}: "
                f"{params.get('reason', '')}",
            )

    elif action_type == "create_support_ticket":
        record["record_id"] = f"TKT-NEW-{seq}"
        record["summary"] = (
            f"Support ticket {record['record_id']} opened for "
            f"{params.get('account_id')}: {params.get('subject', '')}"
        )

    elif action_type == "update_ticket":
        patch = {}
        if params.get("status"):
            patch["status"] = params["status"]
        if params.get("assigned_to"):
            patch["assigned_to"] = params["assigned_to"]
        updated = datastore.update_ticket(
            params["ticket_id"], patch, note=params.get("note")
        )
        record["record_id"] = params["ticket_id"]
        record["summary"] = f"Ticket {params['ticket_id']} updated: {patch or 'note added'}"
        record["ticket_after_update"] = {
            k: updated.get(k) for k in ("ticket_id", "status", "assigned_to", "internal_notes")
        }

    elif action_type == "create_followup_task":
        record["record_id"] = f"TASK-{seq}"
        record["summary"] = f"Follow-up task {record['record_id']}: {params.get('title', '')}"

    elif action_type == "apply_service_credit":
        record["record_id"] = f"CRD-{seq}"
        record["summary"] = (
            f"Service credit of INR {params['amount_inr']} applied to "
            f"{params.get('account_id')} (order {params.get('order_id', 'n/a')}) — mocked ledger entry"
        )

    with _log_lock:
        _action_log.append(record)
    return record


def action_log(principal: Principal) -> list[dict]:
    with _log_lock:
        entries = list(_action_log)
    if principal.account_scope is not None:
        entries = [e for e in entries if e["params"].get("account_id") == principal.account_scope]
    return entries


def reset_log() -> None:
    with _log_lock:
        _action_log.clear()
