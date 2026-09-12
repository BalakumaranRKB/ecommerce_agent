"""
Token and Cost Ledger (Phase 7 / spec §2.8, SPEC-COST).

WHAT THIS IS
------------
An SQLite-backed ledger that tracks token usage and dollar costs across EVERY
LLM API call, partitioned by ticket ID, ticket type, and processing step.

KEY PROPERTIES (§8.1)
---------------------
1. CENTRAL SEAM: Hooked into `llm.Provider.create()` — one insertion point
   capturing 100% of LLM calls across all providers (Bedrock Claude, Anthropic, Groq).
2. PII HYGIENE (§8.1 / §4.2): Non-observability storage must never store raw user
   text. The ledger records only IDs, counts, models, and cost numbers.
3. DETAILED ANALYTICS (§8.2): Provides aggregations for total spend, per-ticket-type
   breakdowns, and pinpoints the most expensive execution path.

Run standalone report demo:
    uv run python cost_ledger.py
"""

from __future__ import annotations

import contextvars
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

DEFAULT_DB_PATH = os.getenv("COST_LEDGER_DB", "cost_ledger.db")

# Model pricing in USD per 1,000,000 tokens (Standard published rates)
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Anthropic Claude 3.5 / 3.7 / 4.6 Sonnet on Bedrock
    "global.anthropic.claude-sonnet-4-6": {"input_per_million": 3.00, "output_per_million": 15.00},
    "claude-sonnet-5": {"input_per_million": 3.00, "output_per_million": 15.00},
    "claude-3-5-sonnet-20241022": {"input_per_million": 3.00, "output_per_million": 15.00},
    # Anthropic Claude Haiku
    "claude-haiku-4.5": {"input_per_million": 0.80, "output_per_million": 4.00},
    "claude-3-5-haiku-20241022": {"input_per_million": 0.80, "output_per_million": 4.00},
    # Groq / Open Source Models
    "openai/gpt-oss-120b": {"input_per_million": 0.15, "output_per_million": 0.60},
    # Default fallback rate
    "default": {"input_per_million": 3.00, "output_per_million": 15.00},
}

# Context variables to automatically associate LLM calls with active ticket/step
_current_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "ledger_current_context", default={"ticket_id": "untracked", "ticket_type": "unclassified", "step": "direct_call"}
)


@contextmanager
def ledger_context(
    ticket_id: str,
    ticket_type: str = "unclassified",
    step: str = "agent_turn",
) -> Iterator[None]:
    """Context manager to attach ticket and step metadata to all downstream LLM calls."""
    token = _current_context.set({
        "ticket_id": ticket_id,
        "ticket_type": ticket_type,
        "step": step,
    })
    try:
        yield
    finally:
        _current_context.reset(token)


def get_current_context() -> dict[str, str]:
    """Return the currently active ticket and step context."""
    return _current_context.get()


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate estimated cost in USD based on input/output tokens and model pricing."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
    input_cost = (input_tokens / 1_000_000.0) * pricing["input_per_million"]
    output_cost = (output_tokens / 1_000_000.0) * pricing["output_per_million"]
    return input_cost + output_cost


def _get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or os.getenv("COST_LEDGER_DB", DEFAULT_DB_PATH)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cost_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            ticket_type TEXT NOT NULL,
            step TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            timestamp REAL NOT NULL
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_ticket_id ON cost_ledger(ticket_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_ticket_type ON cost_ledger(ticket_type);")
    conn.commit()
    return conn


def record_llm_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    ticket_id: str | None = None,
    ticket_type: str | None = None,
    step: str | None = None,
    db_path: str | None = None,
) -> float:
    """Record an LLM call into the ledger. Returns the calculated cost in USD.
    
    Zero text is stored — only token counts, IDs, model name, and computed cost.
    """
    ctx = get_current_context()
    t_id = ticket_id or ctx.get("ticket_id", "untracked")
    t_type = ticket_type or ctx.get("ticket_type", "unclassified")
    st = step or ctx.get("step", "direct_call")

    cost = calculate_cost(model=model, input_tokens=input_tokens, output_tokens=output_tokens)
    ts = time.time()

    conn = _get_connection(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO cost_ledger (
                    ticket_id, ticket_type, step, model,
                    input_tokens, output_tokens, cost_usd, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (t_id, t_type, st, model, input_tokens, output_tokens, cost, ts),
            )
    finally:
        conn.close()

    return cost


def get_summary(db_path: str | None = None) -> dict[str, Any]:
    """Return overall summary of token usage and costs."""
    conn = _get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                COUNT(*) as total_calls,
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens,
                COALESCE(SUM(cost_usd), 0.0) as total_cost_usd
            FROM cost_ledger;
        """)
        row = cur.fetchone()
        return {
            "total_calls": row[0],
            "total_input_tokens": row[1],
            "total_output_tokens": row[2],
            "total_tokens": row[3],
            "total_cost_usd": round(row[4], 6),
        }
    finally:
        conn.close()


def get_spend_by_ticket_type(db_path: str | None = None) -> list[dict[str, Any]]:
    """Return breakdown of token spend aggregated by ticket type."""
    conn = _get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                ticket_type,
                COUNT(*) as call_count,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                SUM(input_tokens + output_tokens) as total_tokens,
                SUM(cost_usd) as total_cost_usd
            FROM cost_ledger
            GROUP BY ticket_type
            ORDER BY total_cost_usd DESC;
        """)
        results = []
        for row in cur.fetchall():
            results.append({
                "ticket_type": row[0],
                "call_count": row[1],
                "input_tokens": row[2],
                "output_tokens": row[3],
                "total_tokens": row[4],
                "total_cost_usd": round(row[5], 6),
            })
        return results
    finally:
        conn.close()


def get_most_expensive_path(db_path: str | None = None) -> dict[str, Any]:
    """Identify the specific ticket type and step combination that incurred the highest cost."""
    conn = _get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                ticket_type,
                step,
                COUNT(*) as call_count,
                SUM(input_tokens + output_tokens) as total_tokens,
                SUM(cost_usd) as total_cost_usd
            FROM cost_ledger
            GROUP BY ticket_type, step
            ORDER BY total_cost_usd DESC
            LIMIT 1;
        """)
        row = cur.fetchone()
        if not row:
            return {"ticket_type": "none", "step": "none", "call_count": 0, "total_tokens": 0, "total_cost_usd": 0.0}
        return {
            "ticket_type": row[0],
            "step": row[1],
            "call_count": row[2],
            "total_tokens": row[3],
            "total_cost_usd": round(row[4], 6),
        }
    finally:
        conn.close()


def reset_ledger(db_path: str | None = None) -> None:
    """Clear all records from the ledger database (useful for test isolation)."""
    conn = _get_connection(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM cost_ledger;")
    finally:
        conn.close()


if __name__ == "__main__":
    print("=== Cost Ledger Report (spec §2.8 / SPEC-COST) ===")
    
    db_path = DEFAULT_DB_PATH
    summary = get_summary(db_path=db_path)
    
    # If the ledger is fresh/empty, seed a representative test run across all 4 ticket types
    if summary["total_calls"] == 0:
        print("[ledger] Seeding baseline test tickets into cost_ledger.db...")
        workload = [
            ("tkt_8702", "order_status", "classify", "claude-sonnet-5", 150, 5),
            ("tkt_8702", "order_status", "agent_turn", "claude-sonnet-5", 850, 120),
            ("tkt_8801", "delivery", "classify", "claude-sonnet-5", 140, 5),
            ("tkt_8801", "delivery", "agent_turn", "claude-sonnet-5", 920, 180),
            ("tkt_8825", "refund", "classify", "claude-sonnet-5", 160, 5),
            ("tkt_8825", "refund", "handoff_decision", "claude-sonnet-5", 1200, 240),
            ("tkt_8825", "refund", "agent_turn", "claude-sonnet-5", 1100, 210),
            ("tkt_8850", "account", "classify", "claude-sonnet-5", 145, 5),
            ("tkt_8850", "account", "agent_turn", "claude-sonnet-5", 890, 130),
        ]
        for tid, ttype, step, model, inp, outp in workload:
            record_llm_call(
                ticket_id=tid, ticket_type=ttype, step=step, model=model,
                input_tokens=inp, output_tokens=outp, db_path=db_path
            )
        summary = get_summary(db_path=db_path)

    print(f"\nTotal LLM Calls : {summary['total_calls']}")
    print(f"Total Tokens    : {summary['total_tokens']:,} (Input: {summary['total_input_tokens']:,}, Output: {summary['total_output_tokens']:,})")
    print(f"Total Spend     : ${summary['total_cost_usd']:.6f} USD\n")

    print("Spend by Ticket Type:")
    for row in get_spend_by_ticket_type(db_path=db_path):
        print(f"  - {row['ticket_type']:<14} : {row['call_count']} calls | {row['total_tokens']:,} tokens | ${row['total_cost_usd']:.6f} USD")

    most_exp = get_most_expensive_path(db_path=db_path)
    print(f"\nMost Expensive Path : {most_exp['ticket_type']} / {most_exp['step']} (${most_exp['total_cost_usd']:.6f} USD)")
