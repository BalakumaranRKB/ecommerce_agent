"""
Phase 7 / spec §2.8 — Cost Ledger Tests (SPEC-COST).

Verifies:
1. Token and cost recording across models and steps.
2. Context manager attachment to active ticket and step.
3. Aggregations: total summary, spend by ticket type, and most expensive path.
4. PII Hygiene (§8.1 / §4.2): SQLite schema contains zero free-text / prompt fields.

Run:
    uv run python test_cost_ledger.py
"""

from __future__ import annotations

import sqlite3
import tempfile

from cost_ledger import (
    calculate_cost,
    get_most_expensive_path,
    get_spend_by_ticket_type,
    get_summary,
    ledger_context,
    record_llm_call,
    reset_ledger,
)


def _temp_db() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def test_cost_calculation():
    """Verify standard pricing math."""
    # 1000 input tokens at $3.00/1M = $0.003, 200 output tokens at $15.00/1M = $0.003 -> Total $0.006
    cost = calculate_cost("claude-sonnet-5", input_tokens=1000, output_tokens=200)
    assert abs(cost - 0.006) < 1e-6, f"Expected 0.006, got {cost}"
    print("  [PASS] test_cost_calculation")


def test_record_and_summary():
    """Record LLM calls and assert accurate aggregations."""
    db_path = _temp_db()
    reset_ledger(db_path)

    record_llm_call(
        ticket_id="tkt_001",
        ticket_type="order_status",
        step="classify",
        model="claude-sonnet-5",
        input_tokens=150,
        output_tokens=10,
        db_path=db_path,
    )
    record_llm_call(
        ticket_id="tkt_001",
        ticket_type="order_status",
        step="agent_turn",
        model="claude-sonnet-5",
        input_tokens=850,
        output_tokens=140,
        db_path=db_path,
    )

    summary = get_summary(db_path=db_path)
    assert summary["total_calls"] == 2
    assert summary["total_input_tokens"] == 1000
    assert summary["total_output_tokens"] == 150
    assert summary["total_tokens"] == 1150
    assert summary["total_cost_usd"] > 0
    print("  [PASS] test_record_and_summary")


def test_context_manager_attachment():
    """Verify ledger_context automatically attaches ticket and step metadata."""
    db_path = _temp_db()
    reset_ledger(db_path)

    with ledger_context(ticket_id="tkt_test_ctx", ticket_type="refund", step="specialist_handoff"):
        record_llm_call(
            model="claude-sonnet-5",
            input_tokens=500,
            output_tokens=50,
            db_path=db_path,
        )

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT ticket_id, ticket_type, step, input_tokens, output_tokens FROM cost_ledger;")
    row = cur.fetchone()
    conn.close()

    assert row == ("tkt_test_ctx", "refund", "specialist_handoff", 500, 50)
    print("  [PASS] test_context_manager_attachment")


def test_spend_by_ticket_type_and_most_expensive_path():
    """Verify grouping and most expensive execution path identification."""
    db_path = _temp_db()
    reset_ledger(db_path)

    # refund path (heavy)
    record_llm_call("claude-sonnet-5", 2000, 500, ticket_id="tkt_ref1", ticket_type="refund", step="agent_turn", db_path=db_path)
    # order_status path (light)
    record_llm_call("claude-sonnet-5", 300, 50, ticket_id="tkt_ord1", ticket_type="order_status", step="agent_turn", db_path=db_path)
    # delivery path (medium)
    record_llm_call("claude-sonnet-5", 800, 100, ticket_id="tkt_del1", ticket_type="delivery", step="agent_turn", db_path=db_path)

    by_type = get_spend_by_ticket_type(db_path=db_path)
    assert len(by_type) == 3
    assert by_type[0]["ticket_type"] == "refund"  # Highest spend first

    most_exp = get_most_expensive_path(db_path=db_path)
    assert most_exp["ticket_type"] == "refund"
    assert most_exp["step"] == "agent_turn"
    print("  [PASS] test_spend_by_ticket_type_and_most_expensive_path")


def test_pii_hygiene_schema():
    """§8.1 / §4.2 Egress: verify ledger schema stores counts and IDs only, never text."""
    db_path = _temp_db()
    reset_ledger(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(cost_ledger);")
    columns = [row[1] for row in cur.fetchall()]
    conn.close()

    forbidden = {"prompt", "response", "text", "message", "query", "user_message", "content"}
    for col in columns:
        assert col.lower() not in forbidden, f"Forbidden text column found in cost ledger: {col}"

    print("  [PASS] test_pii_hygiene_schema (no raw text stored in cost ledger)")


if __name__ == "__main__":
    print("=== Running Cost Ledger Test Suite ===")
    test_cost_calculation()
    test_record_and_summary()
    test_context_manager_attachment()
    test_spend_by_ticket_type_and_most_expensive_path()
    test_pii_hygiene_schema()
    print("\nAll Cost Ledger tests PASSED (5/5).")
