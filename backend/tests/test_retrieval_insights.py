"""Retrieval quality on the designed questions + Ops Radar findings."""

from app import insights, retrieval


def test_cancellation_query_surfaces_sop_and_agreement_for_northstar():
    res = retrieval.search("cancel BOOKED shipment cancellation fee", "ACCT-001")
    doc_ids = [r["doc_id"] for r in res["results"]]
    assert "cancellation-sop-v4" in doc_ids
    assert "northstar-agreement" in doc_ids


def test_bulk_upload_query_finds_known_issue():
    res = retrieval.search("bulk upload CSV rows failure", "ACCT-002")
    assert res["results"][0]["doc_id"] == "product-ops-guide"


def test_results_carry_authority_metadata():
    res = retrieval.search("service credit threshold", "ACCT-002")
    top = res["results"][0]
    assert {"authority_tier", "status", "authority"} <= set(top)
    assert "precedence_note" in res


def test_ops_overview_flags_the_designed_signals():
    o = insights.ops_overview()

    breached = {r["ticket_id"] for r in o["sla_board"] if r["breached"]}
    assert "TKT-501" in breached  # Northstar 15-min 24x7 P1
    assert "TKT-505" in breached  # Axis default 30-min 24x7 P1 (security)
    assert "TKT-502" not in breached  # LumenWorks business-hours clock hasn't started (Sunday)

    clusters = {c["issue_id"]: c for c in o["known_issue_clusters"]}
    assert "KI-208" in clusters  # bulk upload cluster (TKT-502 + closed TKT-451)
    tkt_ids = {t["ticket_id"] for t in clusters["KI-208"]["tickets"]}
    assert {"TKT-502", "TKT-451"} <= tkt_ids
    # Both bulk-upload tickets are LumenWorks: a repeat-complaint cluster, not multi-account.
    assert clusters["KI-208"]["multi_account"] is False
    assert clusters["KI-208"]["accounts_affected"] == ["LumenWorks"]
    assert "KI-211" in clusters  # SwiftShip webhook delay (TKT-504)

    kinds = {a["kind"] for a in o["attention_items"]}
    assert "eligible_service_credit_unactioned" in kinds  # ORD-2002 credit owed
    assert "pending_cancellation_request" in kinds
    assert "order_status_may_be_stale" in kinds  # ORD-1001 SwiftShip caveat

    hist = {h["ticket_id"] for h in o["historical_answer_risks"]}
    assert {"TKT-450", "TKT-451"} <= hist


def test_severity_suggestions():
    o = insights.ops_overview()
    sev = {r["ticket_id"]: r["suggested_severity"] for r in o["sla_board"]}
    assert sev["TKT-501"] == "P1"  # total shipment-creation outage
    assert sev["TKT-505"] == "P1"  # credential exposure
    assert sev["TKT-503"] == "P3"  # billing contact change
