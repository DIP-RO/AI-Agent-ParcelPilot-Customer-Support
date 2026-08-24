"""Proactive issue detection for internal support/operations users.

Deterministic analytics over the ticket/order data joined with the known-issue
registry and the SLA engine. Powers the "Ops Radar" dashboard and the
get_ops_overview agent tool. Heuristics are transparent (each finding lists its
evidence) so the team can trust — and audit — why something was flagged.
"""

from __future__ import annotations

import re

from . import datastore, rules
from .timeutil import fmt_ts, humanize_delta, parse_ts

# Keyword heuristics for *suggested* severity (final classification is human /
# agent judgment against Support Policy v3 §2 — these only prioritise the queue).
_P1_PATTERNS = [
    r"\ball\b.*\bfail", r"every user", r"outage", r"http 500",
    r"api key", r"credential", r"security", r"expos", r"breach",
]
_P2_PATTERNS = [r"bulk upload", r"fail", r"degrad", r"error", r"broken"]


def _suggest_severity(ticket: dict) -> tuple[str, str]:
    text = f"{ticket.get('subject', '')} {ticket.get('description', '')}".lower()
    for pat in _P1_PATTERNS:
        if re.search(pat, text):
            return "P1", f"matched high-severity signal /{pat}/"
    for pat in _P2_PATTERNS:
        if re.search(pat, text):
            return "P2", f"matched degradation signal /{pat}/"
    return "P3", "no outage/degradation signals; likely how-to or config request"


def _match_known_issues(ticket: dict) -> list[dict]:
    text = f"{ticket.get('subject', '')} {ticket.get('description', '')}".lower()
    matches = []
    for ki in datastore.known_issues():
        hits = [kw for kw in ki["match_keywords"] if kw in text]
        if len(hits) >= 2:
            entry = {"issue_id": ki["issue_id"], "title": ki["title"], "status": ki["status"], "matched_on": hits}
            if ki["status"] == "resolved":
                entry["caution"] = ki.get("caution")
            matches.append(entry)
    return matches


def ops_overview() -> dict:
    now = datastore.snapshot_now()
    open_tickets = [
        t for t in datastore.list_tickets(account_scope=None)
        if (t.get("status") or "open") not in ("closed", "resolved")
    ]
    accounts = {a["account_id"]: a for a in datastore.list_accounts(account_scope=None)}

    # --- SLA board: every open ticket, suggested severity, breach state -----
    sla_board = []
    for t in open_tickets:
        severity, why = _suggest_severity(t)
        sla = rules.sla_for(t, accounts[t["account_id"]])
        row = sla["targets_by_severity"][severity]
        age = humanize_delta(now - parse_ts(t["created_at"]))
        sla_board.append({
            "ticket_id": t["ticket_id"],
            "account": accounts[t["account_id"]]["account_name"],
            "subject": t["subject"],
            "age": age,
            "suggested_severity": severity,
            "severity_basis": why,
            "first_response_target": row["target"],
            "due_at": row["due_at"],
            "breached": row["breached_at_reference_time"],
            "margin": row["margin"],
            "sla_source": sla["sla_source"],
            "escalated": bool(t.get("escalated")),
        })
    sla_board.sort(key=lambda r: (not r["breached"], r["suggested_severity"], r["due_at"]))

    # --- Known-issue clusters: tickets mapped to product issues ------------
    clusters: dict[str, dict] = {}
    for t in datastore.list_tickets(account_scope=None):
        for m in _match_known_issues(t):
            c = clusters.setdefault(m["issue_id"], {
                "issue_id": m["issue_id"], "title": m["title"], "issue_status": m["status"],
                "tickets": [], "accounts_affected": set(),
            })
            c["tickets"].append({
                "ticket_id": t["ticket_id"],
                "account": accounts[t["account_id"]]["account_name"],
                "subject": t["subject"],
                "status": t.get("status") or "open",
                "matched_on": m["matched_on"],
            })
            c["accounts_affected"].add(accounts[t["account_id"]]["account_name"])
    cluster_list = []
    for c in clusters.values():
        c["accounts_affected"] = sorted(c["accounts_affected"])
        c["multi_account"] = len(c["accounts_affected"]) > 1
        ki = next(k for k in datastore.known_issues() if k["issue_id"] == c["issue_id"])
        c["workaround"] = ki.get("workaround") or ki.get("guidance")
        cluster_list.append(c)
    cluster_list.sort(key=lambda c: (-len(c["tickets"]), c["issue_id"]))

    # --- Unactioned entitlements & order anomalies -------------------------
    attention: list[dict] = []
    for o in datastore.list_orders(account_scope=None):
        credit = rules.evaluate_service_credit(o["order_id"], account_scope=None)
        if credit.get("eligible"):
            attention.append({
                "kind": "eligible_service_credit_unactioned",
                "order_id": o["order_id"],
                "account": accounts[o["account_id"]]["account_name"],
                "detail": (
                    f"Order {o['order_id']} qualifies for an INR {credit['credit_inr']:.0f} "
                    f"failed-pickup credit ({credit['delay_past_window']} past window, carrier fault) "
                    "but no credit has been applied."
                ),
                "suggested_action": "apply_service_credit",
            })
        if o.get("cancellation_requested_at") and o["status"] == "BOOKED":
            cxl = rules.evaluate_cancellation(o["order_id"], account_scope=None)
            attention.append({
                "kind": "pending_cancellation_request",
                "order_id": o["order_id"],
                "account": accounts[o["account_id"]]["account_name"],
                "detail": (
                    f"Cancellation requested at {o['cancellation_requested_at']} is still "
                    f"unprocessed. Engine verdict: allowed={cxl['allowed']}, "
                    f"fee=INR {cxl.get('fee_inr')}."
                ),
                "suggested_action": "process cancellation per engine verdict",
            })
        if o.get("data_caveats"):
            attention.append({
                "kind": "order_status_may_be_stale",
                "order_id": o["order_id"],
                "account": accounts[o["account_id"]]["account_name"],
                "detail": o["data_caveats"][0],
                "suggested_action": "verify carrier status before customer-facing statements",
            })

    # --- Historical answers that contradict current rules ------------------
    contradictions = []
    for t in datastore.list_tickets(account_scope=None):
        if not t.get("historical_resolution"):
            continue
        contradictions.append({
            "ticket_id": t["ticket_id"],
            "account": accounts[t["account_id"]]["account_name"],
            "historical_resolution": t["historical_resolution"],
            "warning": (
                "Historical resolutions are context only and may be wrong; re-verify "
                "against the signed agreement and current policy before repeating them."
            ),
        })

    breached = [r for r in sla_board if r["breached"]]
    summary = (
        f"{len(open_tickets)} open tickets; {len(breached)} likely past their "
        f"first-response target ({', '.join(r['ticket_id'] for r in breached) or 'none'}). "
        f"{len(cluster_list)} known-issue cluster(s); "
        f"{sum(1 for a in attention if a['kind'] == 'eligible_service_credit_unactioned')} "
        f"unactioned service credit(s)."
    )

    return {
        "reference_time": fmt_ts(now),
        "summary": summary,
        "sla_board": sla_board,
        "known_issue_clusters": cluster_list,
        "attention_items": attention,
        "historical_answer_risks": contradictions,
        "caveats": [
            "Suggested severities are keyword heuristics to prioritise the queue; "
            "confirm against Support Policy v3 §2 before acting.",
            "Breach flags assume no first response has been sent (the dataset has no "
            "response timestamps).",
        ],
    }
