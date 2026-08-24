"""Structured data access with account scoping enforced at the data layer.

Every read goes through this module. Functions take an ``account_scope``:
``None`` means unrestricted (authorised ParcelPilot staff); an account id means
the caller may only ever see that account's rows. Scoping here — rather than in
the prompt — is what makes the privacy guarantee real: the model cannot leak
data it was never given.

Mutations (ticket updates from confirmed actions) live in an in-memory overlay
on top of the immutable dataset. On serverless hosting the overlay is
per-instance and resets on cold start — acceptable for mocked actions, and
called out in the architecture note.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from functools import lru_cache
from typing import Any

from . import config
from .timeutil import IST, fmt_ts, parse_ts


def _load(name: str) -> Any:
    path = config.STRUCTURED_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _raw() -> dict[str, Any]:
    return {
        "accounts": _load("accounts"),
        "orders": _load("orders"),
        "tickets": _load("tickets"),
        "readme": _load("readme"),
        "known_issues": _load("known_issues"),
        "personas": _load("personas"),
        "contract_terms": {k: v for k, v in _load("contract_terms").items() if not k.startswith("_")},
        "policy_defaults": {k: v for k, v in _load("policy_defaults").items() if not k.startswith("_")},
        "doc_registry": _load("doc_registry"),
    }


# ---------------------------------------------------------------------------
# Snapshot clock

@lru_cache(maxsize=1)
def snapshot_now() -> datetime:
    """Reference time from the workbook README (e.g. '2026-08-16 11:00 Asia/Kolkata')."""
    for row in _raw()["readme"]:
        values = " ".join(str(v) for v in row.values() if v)
        m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", values)
        if m and "snapshot" in values.lower():
            return parse_ts(m.group(1))
    raise RuntimeError("Dataset snapshot time not found in workbook README sheet")


# ---------------------------------------------------------------------------
# Scope enforcement

class AccessDenied(Exception):
    """Raised when a caller requests data outside its account scope."""


def _in_scope(record_account: str, account_scope: str | None) -> bool:
    return account_scope is None or record_account == account_scope


# ---------------------------------------------------------------------------
# Accounts

def get_account(account_id: str, account_scope: str | None) -> dict:
    # Out-of-scope reads return the SAME not-found error as a missing id, so a
    # scoped customer cannot distinguish "exists but not yours" from "does not
    # exist" and enumerate other accounts' ids.
    for acct in _raw()["accounts"]:
        if acct["account_id"] == account_id and _in_scope(account_id, account_scope):
            return dict(acct)
    raise KeyError(f"No account with id {account_id!r}")


def list_accounts(account_scope: str | None) -> list[dict]:
    accounts = _raw()["accounts"]
    if account_scope is not None:
        accounts = [a for a in accounts if a["account_id"] == account_scope]
    return [dict(a) for a in accounts]


# ---------------------------------------------------------------------------
# Orders

def _annotate_order(order: dict) -> dict:
    """Attach data-reliability caveats derived from current known issues."""
    order = dict(order)
    caveats = []
    for ki in _raw()["known_issues"]:
        if (
            ki.get("carrier")
            and ki["status"] != "resolved"
            and order.get("carrier") == ki["carrier"]
            and order.get("status") == "BOOKED"
        ):
            window_start = order.get("pickup_window_start")
            if window_start and parse_ts(window_start) <= snapshot_now():
                caveats.append(
                    f"{ki['issue_id']} ({ki['title']}): {ki['detail']} "
                    f"Guidance: {ki.get('guidance', '')} [{ki['source_clause']}]"
                )
    if caveats:
        order["data_caveats"] = caveats
    return order


def get_order(order_id: str, account_scope: str | None) -> dict:
    for order in _raw()["orders"]:
        if order["order_id"] == order_id and _in_scope(order["account_id"], account_scope):
            return _annotate_order(order)
    raise KeyError(f"No order with id {order_id!r}")


def list_orders(account_scope: str | None, account_id: str | None = None) -> list[dict]:
    orders = _raw()["orders"]
    if account_scope is not None:
        if account_id is not None and account_id != account_scope:
            raise AccessDenied("Access denied: cannot list orders for a different account.")
        account_id = account_scope
    if account_id is not None:
        orders = [o for o in orders if o["account_id"] == account_id]
    return [_annotate_order(o) for o in orders]


# ---------------------------------------------------------------------------
# Tickets (with in-memory mutation overlay)

_overlay_lock = threading.Lock()
_ticket_overlay: dict[str, dict] = {}


def _apply_overlay(ticket: dict) -> dict:
    ticket = dict(ticket)
    with _overlay_lock:
        patch = _ticket_overlay.get(ticket["ticket_id"])
        if patch:
            notes = patch.get("_notes", [])
            ticket.update({k: v for k, v in patch.items() if not k.startswith("_")})
            if notes:
                ticket["internal_notes"] = list(notes)
    if ticket.get("historical_resolution"):
        ticket["historical_resolution_warning"] = (
            "Historical resolution: context only. Past answers may be incorrect and "
            "must be re-verified against the signed agreement and current policy "
            "before being repeated (workbook README; Support Policy v3 §1)."
        )
    return ticket


def update_ticket(ticket_id: str, patch: dict, note: str | None = None) -> dict:
    """Apply a confirmed mutation. Only called by the action executor."""
    base = get_ticket(ticket_id, account_scope=None)
    with _overlay_lock:
        entry = _ticket_overlay.setdefault(ticket_id, {"_notes": []})
        entry.update(patch)
        if note:
            entry["_notes"].append(f"[{fmt_ts(snapshot_now())}] {note}")
    return get_ticket(base["ticket_id"], account_scope=None)


def reset_overlay() -> None:
    with _overlay_lock:
        _ticket_overlay.clear()


def get_ticket(ticket_id: str, account_scope: str | None) -> dict:
    for ticket in _raw()["tickets"]:
        if ticket["ticket_id"] == ticket_id and _in_scope(ticket["account_id"], account_scope):
            return _apply_overlay(ticket)
    raise KeyError(f"No ticket with id {ticket_id!r}")


def list_tickets(
    account_scope: str | None,
    account_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    tickets = _raw()["tickets"]
    if account_scope is not None:
        if account_id is not None and account_id != account_scope:
            raise AccessDenied("Access denied: cannot list tickets for a different account.")
        account_id = account_scope
    if account_id is not None:
        tickets = [t for t in tickets if t["account_id"] == account_id]
    result = [_apply_overlay(t) for t in tickets]
    if status is not None:
        result = [t for t in result if (t.get("status") or "open") == status]
    return result


# ---------------------------------------------------------------------------
# Config datasets

def known_issues() -> list[dict]:
    return [dict(k) for k in _raw()["known_issues"]]


def personas() -> list[dict]:
    return [dict(p) for p in _raw()["personas"]]


def contract_terms(account_id: str) -> dict | None:
    terms = _raw()["contract_terms"].get(account_id)
    return dict(terms) if terms else None


def policy_defaults() -> dict:
    return dict(_raw()["policy_defaults"])


def doc_registry() -> list[dict]:
    return [dict(d) for d in _raw()["doc_registry"]]
