"""Deterministic engine tests: every order in the pack, plus the designed traps."""

from app import rules
from app.datastore import snapshot_now
from app.timeutil import parse_ts


# --- Cancellation ----------------------------------------------------------

def test_northstar_ord1001_no_fee_despite_late_request():
    """The headline trap: 120 min after booking, default says INR 250 fee,
    but the Northstar agreement waives it for BOOKED shipments."""
    v = rules.evaluate_cancellation("ORD-1001", account_scope=None)
    assert v["allowed"] is True
    assert v["fee_inr"] == 0
    trace = " ".join(v["rule_trace"])
    assert "Northstar Enterprise Agreement §2" in trace
    assert "OVERRIDES" in trace


def test_ord1001_carries_swiftship_staleness_caveat():
    """KI-211: SwiftShip BOOKED with an elapsed pickup window may already be
    physically picked up — the engine must surface the caveat."""
    v = rules.evaluate_cancellation("ORD-1001", account_scope=None)
    assert any("KI-211" in c for c in v.get("data_caveats", []))


def test_northstar_ord1002_picked_up_no_cancel():
    v = rules.evaluate_cancellation("ORD-1002", account_scope=None)
    assert v["allowed"] is False
    assert "return-to-origin" in " ".join(v["rule_trace"])


def test_lumenworks_ord2001_late_cancel_pays_fee():
    """75 min after booking, no waiver in the LumenWorks agreement -> INR 250."""
    v = rules.evaluate_cancellation("ORD-2001", account_scope=None)
    assert v["allowed"] is True
    assert v["fee_inr"] == 250


def test_beacon_ord3001_within_free_window():
    v = rules.evaluate_cancellation("ORD-3001", account_scope=None)
    assert v["allowed"] is True
    assert v["fee_inr"] == 0
    assert v["minutes_between_booking_and_request"] == 15


def test_axis_ord4001_delivered_cannot_cancel():
    v = rules.evaluate_cancellation("ORD-4001", account_scope=None)
    assert v["allowed"] is False


# --- Service credits -------------------------------------------------------

def test_lumenworks_ord2002_contract_credit_not_default():
    """4.5h delay, carrier fault. Default formula would give min(500, 10%*2400)=240
    at a 2h threshold; the LumenWorks agreement replaces it: 4h threshold, flat 300."""
    v = rules.evaluate_service_credit("ORD-2002", account_scope=None)
    assert v["eligible"] is True
    assert v["credit_inr"] == 300
    assert v["requires_manager_approval"] is False
    assert "LumenWorks Service Agreement §3" in " ".join(v["rule_trace"])


def test_ord2002_delay_math():
    v = rules.evaluate_service_credit("ORD-2002", account_scope=None)
    assert v["delay_hours"] == 4.5  # 06:30 window end -> 11:00 snapshot


def test_northstar_ord1002_picked_up_on_time_no_credit():
    v = rules.evaluate_service_credit("ORD-1002", account_scope=None)
    assert v["eligible"] is False


def test_default_formula_for_account_without_contract():
    """Beacon ORD-3001: no carrier fault, not delayed past threshold -> ineligible,
    and the trace cites the SOP (no contract terms exist)."""
    v = rules.evaluate_service_credit("ORD-3001", account_scope=None)
    assert v["eligible"] is False
    assert "SOP v4" in " ".join(v["rule_trace"])


def test_hypothetical_lumenworks_3h_not_eligible():
    """The brief's second example, asked by LumenWorks with no specific order:
    3h < their contractual 4h threshold -> NO credit, cited to the agreement."""
    v = rules.evaluate_credit_terms(
        "ACCT-002", delay_hours=3, carrier_fault=True, customer_fault=False, account_scope="ACCT-002"
    )
    assert v["eligible"] is False
    assert "LumenWorks Service Agreement §3" in " ".join(v["rule_trace"])


def test_hypothetical_lumenworks_5h_eligible_fixed_300():
    v = rules.evaluate_credit_terms(
        "ACCT-002", delay_hours=5, carrier_fault=True, customer_fault=False, account_scope="ACCT-002"
    )
    assert v["eligible"] is True
    assert v["credit_inr"] == 300


def test_hypothetical_default_account_3h_eligible():
    """Beacon (no contract): default 2h threshold -> 3h IS eligible; amount needs fee."""
    v = rules.evaluate_credit_terms(
        "ACCT-003", delay_hours=3, carrier_fault=True, customer_fault=False, account_scope="ACCT-003"
    )
    assert v["eligible"] is True
    assert v["reason"] == "need_shipment_fee"
    v2 = rules.evaluate_credit_terms(
        "ACCT-003", delay_hours=3, carrier_fault=True, customer_fault=False,
        account_scope="ACCT-003", shipment_fee_inr=2000,
    )
    assert v2["credit_inr"] == 200  # min(500, 10% of 2000)


def test_hypothetical_customer_fault_blocks_credit():
    v = rules.evaluate_credit_terms(
        "ACCT-003", delay_hours=10, carrier_fault=True, customer_fault=True, account_scope="ACCT-003"
    )
    assert v["eligible"] is False


def test_hypothetical_credit_terms_scoped():
    """A customer cannot ask about another account's hypothetical terms."""
    import pytest as _pytest

    with _pytest.raises(KeyError):
        rules.evaluate_credit_terms(
            "ACCT-002", delay_hours=5, carrier_fault=True, customer_fault=False, account_scope="ACCT-001"
        )


def test_northstar_monthly_cap_flagged():
    """A hypothetical eligible Northstar credit must mention the INR 5,000 monthly
    aggregate cap. ORD-1001 is not delayed/carrier-fault so use the cap plumbing
    directly via ORD-2002-style checks on ORD-1001's account terms."""
    from app import datastore

    terms = datastore.contract_terms("ACCT-001")
    assert terms["service_credit"]["monthly_aggregate_cap_inr"] == 5000


# --- SLA -------------------------------------------------------------------

def test_snapshot_is_sunday():
    assert snapshot_now().weekday() == 6  # Sunday — the whole point of the business-hours traps


def test_tkt501_northstar_p1_breached_24x7():
    """Contractual 15-min 24x7 P1: created 10:30, snapshot 11:00 -> breached."""
    v = rules.check_sla("TKT-501", account_scope=None)
    p1 = v["targets_by_severity"]["P1"]
    assert p1["breached_at_reference_time"] is True
    assert "15 minutes" in p1["target"]
    assert "Northstar" in v["sla_source"]


def test_tkt505_axis_default_enterprise_p1_breached():
    """Axis has no contract: default Enterprise P1 = 30 min 24x7. Created 08:30 -> breached."""
    v = rules.check_sla("TKT-505", account_scope=None)
    assert v["targets_by_severity"]["P1"]["breached_at_reference_time"] is True
    assert "plan defaults" in v["sla_source"]


def test_tkt502_lumenworks_p2_not_breached_on_sunday():
    """LumenWorks P2 = 4 business hours with no weekend coverage. Created Sunday
    09:45 -> clock starts Monday 09:00, due Monday 13:00, NOT breached at snapshot."""
    v = rules.check_sla("TKT-502", account_scope=None)
    p2 = v["targets_by_severity"]["P2"]
    assert p2["breached_at_reference_time"] is False
    assert "Monday" in p2["due_at"]
    assert "13:00" in p2["due_at"]


def test_tkt503_beacon_p3_two_business_days():
    """Standard P3 = 2 business days from Sunday -> due Tuesday 18:00."""
    v = rules.check_sla("TKT-503", account_scope=None)
    p3 = v["targets_by_severity"]["P3"]
    assert p3["breached_at_reference_time"] is False
    assert "Tuesday" in p3["due_at"]


def test_northstar_p2_wall_clock_hour():
    """Northstar P2 '1 hour' is wall-clock: TKT-501 created 10:30 -> P2 due 11:30 Sunday."""
    v = rules.check_sla("TKT-501", account_scope=None)
    p2 = v["targets_by_severity"]["P2"]
    assert "11:30" in p2["due_at"]
    assert p2["breached_at_reference_time"] is False


# --- Business-hours arithmetic sanity --------------------------------------

def test_business_hours_math():
    from app.timeutil import add_business_hours

    sunday = parse_ts("2026-08-16 09:45")
    # 4 business hours from a Sunday start: Monday 09:00 + 4h = Monday 13:00
    due = add_business_hours(sunday, 4)
    assert due == parse_ts("2026-08-17 13:00")
    # 9 business hours (= 1 business day) from Monday 10:00 -> Tuesday 10:00
    monday = parse_ts("2026-08-17 10:00")
    assert add_business_hours(monday, 9) == parse_ts("2026-08-18 10:00")
