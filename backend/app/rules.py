"""Deterministic policy engines: cancellation, service credits, SLA.

Money and deadline decisions are computed in code, never by LLM arithmetic.
Each verdict returns the full rule trace — which clause applied, what it
overrode, and every citation — so the agent can explain the decision and the
answer is reproducible. Terms come from data-pack-derived config:

  policy_defaults.json   defaults from the CURRENT policy/SOP documents
  contract_terms.json    per-account overrides from signed agreements

Per Support Policy v3 §1, contract terms override defaults; anything the
contract doesn't mention falls through to the defaults.
"""

from __future__ import annotations

from datetime import datetime

from . import datastore
from .timeutil import (
    compute_due_at,
    describe_target,
    fmt_ts,
    humanize_delta,
    parse_ts,
)


def _order_and_account(order_id: str, account_scope: str | None) -> tuple[dict, dict]:
    order = datastore.get_order(order_id, account_scope)
    account = datastore.get_account(order["account_id"], account_scope)
    return order, account


# ---------------------------------------------------------------------------
# Cancellation

def evaluate_cancellation(order_id: str, account_scope: str | None) -> dict:
    order, account = _order_and_account(order_id, account_scope)
    defaults = datastore.policy_defaults()["cancellation"]
    contract = datastore.contract_terms(order["account_id"]) or {}
    contract_cxl = contract.get("cancellation") or {}
    now = datastore.snapshot_now()

    status = order["status"]
    trace: list[str] = []
    result: dict = {
        "order_id": order_id,
        "account_id": order["account_id"],
        "account_name": account["account_name"],
        "order_status": status,
        "reference_time": fmt_ts(now),
    }

    if status == "DRAFT":
        result.update(allowed=True, fee_inr=0)
        trace.append(f"DRAFT orders may be cancelled with no fee [{defaults['source_clause']}].")

    elif status == "BOOKED" and not order.get("booked_at"):
        result.update(allowed=None, fee_inr=None, reason="missing_booked_at")
        trace.append(
            "Order is BOOKED but has no booking timestamp, so the free-window fee "
            "rule cannot be evaluated; verify the record before acting "
            f"[{defaults['source_clause']}]."
        )

    elif status == "BOOKED":
        result["allowed"] = True
        booked_at = parse_ts(order["booked_at"])
        requested_raw = order.get("cancellation_requested_at")
        requested_at = parse_ts(requested_raw) if requested_raw else now
        minutes_since_booking = (requested_at - booked_at).total_seconds() / 60
        result["minutes_between_booking_and_request"] = round(minutes_since_booking)
        if not requested_raw:
            trace.append(
                "No cancellation request timestamp on record; evaluated as of the "
                "dataset snapshot time."
            )
        if contract_cxl.get("booked_fee_waived"):
            result["fee_inr"] = 0
            trace.append(
                f"Signed agreement waives the cancellation fee for BOOKED shipments "
                f"before pickup, regardless of booking age "
                f"[{contract_cxl['source_clause']}]. This OVERRIDES the default "
                f"INR {defaults['booked']['late_fee_inr']} fee after "
                f"{defaults['booked']['free_within_minutes_of_booking']} minutes "
                f"[{defaults['source_clause']}]."
            )
        elif minutes_since_booking <= defaults["booked"]["free_within_minutes_of_booking"]:
            result["fee_inr"] = 0
            trace.append(
                f"Cancellation requested {round(minutes_since_booking)} minutes after "
                f"booking — within the {defaults['booked']['free_within_minutes_of_booking']}-minute "
                f"free window, so no fee [{defaults['source_clause']}]."
            )
        else:
            result["fee_inr"] = defaults["booked"]["late_fee_inr"]
            trace.append(
                f"Cancellation requested {round(minutes_since_booking)} minutes after "
                f"booking — beyond the {defaults['booked']['free_within_minutes_of_booking']}-minute "
                f"free window, so the INR {defaults['booked']['late_fee_inr']} fee applies; "
                f"no agreement waiver exists for this account [{defaults['source_clause']}]."
            )

    elif status == "PICKED_UP":
        result.update(allowed=False, fee_inr=None, alternative=defaults["picked_up"]["alternative"])
        trace.append(
            f"PICKED_UP shipments must not be cancelled; use the "
            f"{defaults['picked_up']['alternative']} if the customer wants the parcel "
            f"returned [{defaults['source_clause']}]."
        )

    elif status == "DELIVERED":
        result.update(allowed=False, fee_inr=None)
        trace.append(f"DELIVERED shipments cannot be cancelled [{defaults['source_clause']}].")

    else:
        result.update(allowed=False, fee_inr=None)
        trace.append(f"Unrecognised order status {status!r}; escalate to a human.")

    if order.get("data_caveats"):
        result["data_caveats"] = order["data_caveats"]
    result["rule_trace"] = trace
    return result


# ---------------------------------------------------------------------------
# Service credits (failed pickup)

def evaluate_service_credit(order_id: str, account_scope: str | None) -> dict:
    order, account = _order_and_account(order_id, account_scope)
    defaults = datastore.policy_defaults()["service_credit"]
    contract = datastore.contract_terms(order["account_id"]) or {}
    contract_credit = contract.get("service_credit") or {}
    now = datastore.snapshot_now()

    result: dict = {
        "order_id": order_id,
        "account_id": order["account_id"],
        "account_name": account["account_name"],
        "order_status": order["status"],
        "carrier": order["carrier"],
        "reference_time": fmt_ts(now),
    }
    trace: list[str] = []

    # Applicable terms: contract overrides threshold/amount where present.
    threshold_h = contract_credit.get("delay_threshold_hours", defaults["delay_threshold_hours"])
    if "delay_threshold_hours" in contract_credit or "fixed_amount_inr" in contract_credit:
        terms_clause = contract_credit["source_clause"]
        trace.append(
            f"This account has contract-specific failed-pickup credit terms which "
            f"replace the SOP defaults [{terms_clause}]."
        )
    else:
        terms_clause = defaults["source_clause"]

    # Delay past the scheduled pickup window end.
    window_end_raw = order.get("pickup_window_end")
    if not window_end_raw:
        result.update(eligible=False, reason="no_pickup_window")
        trace.append("Order has no scheduled pickup window on record; cannot assess a failed-pickup credit.")
        result["rule_trace"] = trace
        return result
    window_end = parse_ts(window_end_raw)
    picked_at = parse_ts(order["pickup_actual_at"]) if order.get("pickup_actual_at") else None
    measured_to = picked_at or now
    delay = measured_to - window_end
    delay_hours = delay.total_seconds() / 3600
    result["pickup_window_end"] = fmt_ts(window_end)
    result["delay_past_window"] = humanize_delta(delay)
    result["delay_hours"] = round(delay_hours, 2)
    result["delay_measured_to"] = (
        f"actual pickup at {fmt_ts(picked_at)}" if picked_at else "dataset snapshot time (pickup still pending)"
    )

    carrier_fault = order.get("carrier_fault")
    customer_fault = order.get("customer_fault")
    result["carrier_fault"] = carrier_fault
    result["customer_fault"] = customer_fault

    # SOP v4 §3: never promise a credit when fault or timing is unknown.
    if carrier_fault is None or customer_fault is None:
        result.update(eligible=None, reason="fault_unknown")
        trace.append(
            "Carrier/customer fault is not recorded. Do not promise a credit when "
            "fault is unknown — verify before any state-changing action "
            f"[{defaults['manager_approval_clause']}]."
        )
        result["rule_trace"] = trace
        return result

    conditions = {
        f"delay > {threshold_h}h past pickup window end": delay_hours > threshold_h,
        "carrier at fault": bool(carrier_fault),
        "no customer-caused issue": not customer_fault,
    }
    result["conditions"] = conditions

    if all(conditions.values()) and "fixed_amount_inr" not in contract_credit and order.get("shipment_fee_inr") is None:
        # Default formula needs the shipment fee; without it, don't guess an amount.
        result.update(eligible=None, reason="missing_shipment_fee")
        trace.append(
            "Conditions for a credit are met, but the shipment fee is missing so the "
            f"default '{defaults['credit_formula']}' cannot be computed — verify the "
            f"fee before applying a credit [{defaults['source_clause']}]."
        )
        result["rule_trace"] = trace
        return result

    if all(conditions.values()):
        if "fixed_amount_inr" in contract_credit:
            amount = contract_credit["fixed_amount_inr"]
            trace.append(
                f"All conditions met; contract grants a fixed INR {amount} credit "
                f"(replacing the default '{defaults['credit_formula']}') [{terms_clause}]."
            )
        else:
            pct_amount = order["shipment_fee_inr"] * defaults["credit_pct_of_fee"] / 100
            amount = min(defaults["credit_cap_inr"], pct_amount)
            trace.append(
                f"All conditions met; default credit is the {defaults['credit_formula']}: "
                f"min(INR {defaults['credit_cap_inr']}, "
                f"{defaults['credit_pct_of_fee']}% of INR {order['shipment_fee_inr']:.0f} = "
                f"INR {pct_amount:.0f}) = INR {amount:.0f} [{defaults['source_clause']}]."
            )
        result.update(eligible=True, credit_inr=round(amount, 2))
        needs_approval = amount > defaults["manager_approval_above_inr"]
        result["requires_manager_approval"] = needs_approval
        if needs_approval:
            trace.append(
                f"Credit exceeds INR {defaults['manager_approval_above_inr']} and "
                f"requires manager approval [{defaults['manager_approval_clause']}]."
            )
        if contract_credit.get("monthly_aggregate_cap_inr") or (
            (datastore.contract_terms(order["account_id"]) or {})
            .get("service_credit", {})
            .get("monthly_aggregate_cap_inr")
        ):
            cap = (datastore.contract_terms(order["account_id"]) or {})["service_credit"][
                "monthly_aggregate_cap_inr"
            ]
            result["monthly_aggregate_cap_inr"] = cap
            trace.append(
                f"Note: this account's monthly aggregate service credits are capped at "
                f"INR {cap}; month-to-date credit totals are not in this dataset, so "
                f"verify the aggregate before applying."
            )
    else:
        failed = [name for name, ok in conditions.items() if not ok]
        result.update(eligible=False, credit_inr=0, reason="conditions_not_met")
        trace.append(
            f"Not eligible under {('contract terms' if 'fixed_amount_inr' in contract_credit else 'default SOP terms')} "
            f"[{terms_clause}]: failed condition(s): {', '.join(failed)}."
        )

    if order.get("data_caveats"):
        result["data_caveats"] = order["data_caveats"]
    result["rule_trace"] = trace
    return result


def evaluate_credit_terms(
    account_id: str,
    delay_hours: float,
    carrier_fault: bool,
    customer_fault: bool,
    account_scope: str | None,
    shipment_fee_inr: float | None = None,
) -> dict:
    """Same contract-over-default credit logic as evaluate_service_credit, but for
    a HYPOTHETICAL scenario ("a pickup is 3 hours late, carrier fault — credit?")
    where no concrete order exists. Keeps money decisions in code, not the LLM."""
    account = datastore.get_account(account_id, account_scope)  # enforces scope
    defaults = datastore.policy_defaults()["service_credit"]
    contract = datastore.contract_terms(account_id) or {}
    contract_credit = contract.get("service_credit") or {}

    threshold_h = contract_credit.get("delay_threshold_hours", defaults["delay_threshold_hours"])
    using_contract = "delay_threshold_hours" in contract_credit or "fixed_amount_inr" in contract_credit
    terms_clause = contract_credit["source_clause"] if using_contract else defaults["source_clause"]

    trace: list[str] = []
    result: dict = {
        "account_id": account_id,
        "account_name": account["account_name"],
        "scenario": {
            "delay_hours": delay_hours,
            "carrier_fault": carrier_fault,
            "customer_fault": customer_fault,
        },
        "hypothetical": True,
    }
    if using_contract:
        trace.append(f"This account has contract-specific credit terms that replace the SOP defaults [{terms_clause}].")

    conditions = {
        f"delay > {threshold_h}h past pickup window end": delay_hours > threshold_h,
        "carrier at fault": bool(carrier_fault),
        "no customer-caused issue": not customer_fault,
    }
    result["conditions"] = conditions

    if not all(conditions.values()):
        failed = [name for name, ok in conditions.items() if not ok]
        result.update(eligible=False, credit_inr=0, reason="conditions_not_met")
        trace.append(
            f"Not eligible under {'contract terms' if using_contract else 'default SOP terms'} "
            f"[{terms_clause}]: failed condition(s): {', '.join(failed)}."
        )
        result["rule_trace"] = trace
        return result

    if "fixed_amount_inr" in contract_credit:
        amount = contract_credit["fixed_amount_inr"]
        trace.append(f"Conditions met; contract grants a fixed INR {amount} credit [{terms_clause}].")
        result.update(eligible=True, credit_inr=round(amount, 2))
    elif shipment_fee_inr is None:
        result.update(eligible=True, credit_inr=None, reason="need_shipment_fee")
        trace.append(
            f"Conditions met, but the default credit is the {defaults['credit_formula']} — "
            "provide the shipment fee to compute the exact amount "
            f"[{defaults['source_clause']}]."
        )
    else:
        pct_amount = shipment_fee_inr * defaults["credit_pct_of_fee"] / 100
        amount = min(defaults["credit_cap_inr"], pct_amount)
        trace.append(
            f"Conditions met; default credit = min(INR {defaults['credit_cap_inr']}, "
            f"{defaults['credit_pct_of_fee']}% of INR {shipment_fee_inr:.0f}) = INR {amount:.0f} "
            f"[{defaults['source_clause']}]."
        )
        result.update(eligible=True, credit_inr=round(amount, 2))

    if isinstance(result.get("credit_inr"), (int, float)):
        result["requires_manager_approval"] = result["credit_inr"] > defaults["manager_approval_above_inr"]
    cap = contract_credit.get("monthly_aggregate_cap_inr")
    if cap:
        result["monthly_aggregate_cap_inr"] = cap
        trace.append(f"Note: monthly aggregate credits for this account are capped at INR {cap}.")
    result["rule_trace"] = trace
    return result


# ---------------------------------------------------------------------------
# SLA / first-response targets

def check_sla(ticket_id: str, account_scope: str | None) -> dict:
    ticket = datastore.get_ticket(ticket_id, account_scope)
    account = datastore.get_account(ticket["account_id"], account_scope)
    return sla_for(ticket, account)


def sla_for(ticket: dict, account: dict) -> dict:
    """SLA matrix for a ticket: per-severity targets, due times and breach state."""
    defaults = datastore.policy_defaults()
    contract = datastore.contract_terms(account["account_id"]) or {}
    now = datastore.snapshot_now()
    created = parse_ts(ticket["created_at"])

    contract_sla = contract.get("sla")
    if contract_sla:
        targets = {sev: contract_sla[sev] for sev in ("P1", "P2", "P3")}
        source = contract_sla["source_clause"]
        coverage_notes = contract_sla.get("coverage_notes")
    else:
        plan = account["plan"]
        targets = defaults["sla"]["plans"][plan]
        source = f"{defaults['sla']['source_clause']} — {plan} plan defaults"
        coverage_notes = None

    matrix = {}
    for sev, target in targets.items():
        due = compute_due_at(created, target)
        breached = now > due
        matrix[sev] = {
            "target": describe_target(target),
            "due_at": fmt_ts(due),
            "breached_at_reference_time": breached,
            "margin": (
                f"overdue by {humanize_delta(now - due)}"
                if breached
                else f"{humanize_delta(due - now)} remaining"
            ),
        }

    result = {
        "ticket_id": ticket["ticket_id"],
        "account_id": account["account_id"],
        "account_name": account["account_name"],
        "plan": account["plan"],
        "ticket_status": ticket.get("status") or "open",
        "created_at": fmt_ts(created),
        "reference_time": fmt_ts(now),
        "sla_source": source,
        "severity_definitions_source": defaults["severity_definitions"]["source_clause"],
        "targets_by_severity": matrix,
        "assumptions": [
            "The dataset has no first-response timestamp, so breach status is computed "
            "from ticket creation to the dataset snapshot time assuming no response yet.",
            "Business hours are assumed Mon-Fri 09:00-18:00 IST; the snapshot falls on "
            "a Sunday, so business-hours clocks have not started for weekend-created tickets.",
        ],
        "note": (
            "Severity classification is a judgment call: match the ticket against the "
            "severity definitions in Support Policy v3 §2, then read the row for that "
            "severity. If a target is already breached, state the breach and recommend "
            "escalation (Support Policy v3 §4)."
        ),
    }
    if coverage_notes:
        result["coverage_notes"] = coverage_notes
    return result
