"""
Database access — the one module that knows SQL, so nothing else does.

Same shape as tracing.py: a single seam. `retriever.py`, `mcp_server.py`,
`memory.py`, and `harness.py` call functions from here and never write a query
or import psycopg themselves. When the connection story changes (RDS Proxy,
pooling, a different driver), this file changes and the other four don't.

WHY POSTGRES AT ALL (docs/PLAN-assignment-2.md §5.2):
Assignment 1 kept the policy index inside one Python process and the mock stores
in module-level dicts. That was correct for a single-process CLI. It stops being
correct the moment Stage 11 autoscaling adds a second Fargate task: each task
would build its own private index, pay its own startup embedding cost, and be
unable to see anything the other one wrote. Moving the vectors out of process
memory and into a shared table is what makes >1 replica coherent.

ONE ENGINE, TWO JOBS (§2.6.2):
The same database holds the policy embeddings AND the order/account/history
tables. That is deliberate — it is the assignment's stated reason for choosing
pgvector on RDS over a dedicated vector service: a handful of policy documents
do not justify a second piece of infrastructure.

CONNECTION STRATEGY:
One lazily-created connection per process, reused. Deliberately not pooled:
max_capacity is 4 Fargate tasks (§2.6.3) against db.t4g.micro's connection
limit, so exhaustion is not reachable at this scale. RDS Proxy is the
production answer and is named in PLAN §13 as a known ceiling, not an oversight.

Note the MCP server runs as a SUBPROCESS per tool call, so it gets its own
connection each time. That costs a connect round-trip per call — acceptable
locally, and the honest cost of the stdio-subprocess design carried over from
Assignment 1.

Set up the schema (idempotent):
    uv run python db.py
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()

EMBED_DIM = 384  # all-MiniLM-L6-v2

_conn: psycopg.Connection | None = None


def _conn_kwargs() -> dict:
    """Connection parameters as explicit keyword arguments.

    Passed as kwargs rather than concatenated into a DSN string for two reasons.
    First, a password containing a space, quote, or backslash silently corrupts
    a hand-built DSN -- which matters at Stage 10, where RDS generates a password
    full of punctuation. Second, explicit kwargs take precedence over libpq's
    PG* environment variables (PGPASSWORD, PGUSER, PGHOST), so a stale variable
    left behind by some other Postgres install cannot quietly override what .env
    says. That failure mode is nasty: the credentials look right everywhere you
    check, and the connection still gets refused.

    DB_HOST is the only value that differs between laptop, CI, and RDS.
    """
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "ordercare"),
        "user": os.getenv("DB_USER", "ordercare"),
        "password": os.getenv("DB_PASSWORD", ""),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "2")),
    }


def get_conn() -> psycopg.Connection | None:
    """The process-wide connection, opened on first use and reused after.

    autocommit=True because every statement here is either a one-shot read or
    an idempotent seed write; there is no multi-statement transaction to
    manage, and it keeps callers from having to remember to commit.

    Returns None when Postgres is unreachable (e.g. cloud runtime / mock mode).
    """
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn
    try:
        _conn = psycopg.connect(**_conn_kwargs(), autocommit=True, row_factory=dict_row)
        _register_vector(_conn)
        return _conn
    except Exception:
        return None


def _register_vector(conn: psycopg.Connection) -> None:
    """Teach psycopg the `vector` type so lists round-trip as embeddings.

    Tolerates failure: on a fresh database the extension does not exist yet, and
    init_schema() has to be able to connect in order to create it. Registration
    is retried there once the extension is in place.
    """
    try:
        from pgvector.psycopg import register_vector

        register_vector(conn)
    except Exception:  # noqa: BLE001 — expected before CREATE EXTENSION runs
        pass


# --------------------------------------------------------------------- schema

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS policy_chunks (
    doc_id    TEXT PRIMARY KEY,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL,
    embedding vector(384) NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    customer_id TEXT PRIMARY KEY,
    standing    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id               TEXT PRIMARY KEY,
    customer_id            TEXT NOT NULL REFERENCES accounts(customer_id),
    item                   TEXT NOT NULL,
    status                 TEXT NOT NULL,
    promised_delivery_date TEXT,
    delivery_date          TEXT
);

CREATE TABLE IF NOT EXISTS prior_tickets (
    ticket_id   TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES accounts(customer_id),
    type        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    resolution  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_prior_tickets_customer ON prior_tickets(customer_id);
"""

# NOTE: deliberately NO index on policy_chunks.embedding. At 7 rows a sequential
# scan computes 7 dot products, which beats consulting any ANN structure, and
# IVFFlat needs clustering data it does not have at this size. Same reasoning as
# not building a KD-tree for 7 points. Revisit past a few thousand chunks
# (PLAN §14). The two b-tree indexes above ARE worth it — they serve exact-match
# foreign-key lookups, which is a different question entirely.


def init_schema() -> None:
    """Create the extension and all four tables. Idempotent."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    _register_vector(conn)  # retry now that the extension exists


# ---------------------------------------------------------------- policy_chunks


def search_policy_chunks(
    embedding: list[float],
    k: int,
    min_similarity: float,
) -> list[dict]:
    """Cosine similarity search over the policy corpus, best first.

    pgvector's `<=>` is cosine DISTANCE, so similarity = 1 - distance. That is
    the same quantity Chroma returned, which is why Assignment 1's tuned
    MIN_SIMILARITY = 0.32 carries across untouched — same metric, same scale.

    Filtering in SQL rather than in Python is deliberate: the threshold is part
    of what "retrieved" means, so the database should never hand back a row the
    caller is required to discard.
    """
    with get_conn().cursor() as cur:
        cur.execute(
            """
            SELECT doc_id,
                   title,
                   body,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM policy_chunks
            WHERE 1 - (embedding <=> %s::vector) >= %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, embedding, min_similarity, embedding, k),
        )
        return cur.fetchall()


def upsert_policy_chunk(doc_id: str, title: str, body: str, embedding: list[float]) -> None:
    """Insert or replace one policy chunk. Used by ingest.py."""
    with get_conn().cursor() as cur:
        cur.execute(
            """
            INSERT INTO policy_chunks (doc_id, title, body, embedding)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (doc_id) DO UPDATE
              SET title = EXCLUDED.title,
                  body = EXCLUDED.body,
                  embedding = EXCLUDED.embedding
            """,
            (doc_id, title, body, embedding),
        )


def count_policy_chunks() -> int:
    with get_conn().cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM policy_chunks")
        return cur.fetchone()["n"]


# ----------------------------------------------------------- orders / accounts


def fetch_order(order_id: str) -> dict | None:
    """One order by id, or None. Returns ALL columns including both dates —
    the gap between promised_delivery_date and delivery_date is how a delivery
    ticket gets answered with a number instead of a status word."""
    conn = get_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
                return cur.fetchone()
        except Exception:
            pass
    import mock_data
    return mock_data.ORDERS.get(order_id)


def fetch_order_owner(order_id: str) -> str | None:
    """Which customer owns this order, or None if it does not exist.

    Ownership ONLY — never order contents. The harness calls this before
    dispatch to decide whether a proposed call is in scope, and a policy layer
    should not be handed data it is only meant to make a decision about.
    """
    conn = get_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT customer_id FROM orders WHERE order_id = %s", (order_id,))
                row = cur.fetchone()
                return row["customer_id"] if row else None
        except Exception:
            pass
    import mock_data
    return mock_data.ORDER_OWNER.get(order_id)


def fetch_account(customer_id: str) -> dict | None:
    """Account standing, plus the customer's order ids, shaped like Assignment
    1's ACCOUNTS records so callers are unchanged."""
    conn = get_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM accounts WHERE customer_id = %s", (customer_id,))
                account = cur.fetchone()
                if account is not None:
                    cur.execute(
                        "SELECT order_id FROM orders WHERE customer_id = %s ORDER BY order_id",
                        (customer_id,),
                    )
                    account["order_history"] = [r["order_id"] for r in cur.fetchall()]
                    return account
        except Exception:
            pass
    import mock_data
    acct = mock_data.ACCOUNTS.get(customer_id)
    return dict(acct) if acct is not None else None


def fetch_prior_tickets(customer_id: str) -> list[dict]:
    """This customer's closed tickets, oldest first. [] for a first-time
    customer, which is a normal case and not an error."""
    conn = get_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ticket_id, type, summary, resolution
                    FROM prior_tickets
                    WHERE customer_id = %s
                    ORDER BY ticket_id
                    """,
                    (customer_id,),
                )
                return cur.fetchall()
        except Exception:
            pass
    import mock_data
    return list(mock_data.PRIOR_TICKETS.get(customer_id, []))


# ------------------------------------------------------------------- seeding


def seed_reference_data(accounts: dict, orders: dict, prior_tickets: dict) -> tuple[int, int, int]:
    """Load the mock stores into Postgres. Idempotent; safe to re-run.

    Accounts go first: orders and prior_tickets both carry a foreign key to
    accounts(customer_id), so seeding out of order fails loudly rather than
    silently creating orphans.
    """
    conn = get_conn()
    with conn.cursor() as cur:
        for cust_id, acct in accounts.items():
            cur.execute(
                """
                INSERT INTO accounts (customer_id, standing) VALUES (%s, %s)
                ON CONFLICT (customer_id) DO UPDATE SET standing = EXCLUDED.standing
                """,
                (cust_id, acct["standing"]),
            )

        for order_id, o in orders.items():
            cur.execute(
                """
                INSERT INTO orders (order_id, customer_id, item, status,
                                    promised_delivery_date, delivery_date)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (order_id) DO UPDATE
                  SET customer_id = EXCLUDED.customer_id,
                      item = EXCLUDED.item,
                      status = EXCLUDED.status,
                      promised_delivery_date = EXCLUDED.promised_delivery_date,
                      delivery_date = EXCLUDED.delivery_date
                """,
                (
                    order_id,
                    o["customer_id"],
                    o["item"],
                    o["status"],
                    o.get("promised_delivery_date"),
                    o.get("delivery_date"),
                ),
            )

        n_tickets = 0
        for cust_id, tickets in prior_tickets.items():
            for t in tickets:
                cur.execute(
                    """
                    INSERT INTO prior_tickets (ticket_id, customer_id, type, summary, resolution)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (ticket_id) DO UPDATE
                      SET customer_id = EXCLUDED.customer_id,
                          type = EXCLUDED.type,
                          summary = EXCLUDED.summary,
                          resolution = EXCLUDED.resolution
                    """,
                    (t["ticket_id"], cust_id, t["type"], t["summary"], t["resolution"]),
                )
                n_tickets += 1

    return len(accounts), len(orders), n_tickets


if __name__ == "__main__":
    kw = _conn_kwargs()
    pw_state = "set" if kw["password"] else "EMPTY"
    print(f"[db] connecting: host={kw['host']} port={kw['port']} "
          f"dbname={kw['dbname']} user={kw['user']} password={pw_state}")
    stale = sorted(k for k in os.environ if k.startswith("PG"))
    if stale:
        print(f"[db] libpq PG* vars in environment: {stale}")
        print("[db]   explicit kwargs take precedence, so these are ignored.")
    init_schema()
    with get_conn().cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
        print(f"[db] pgvector extension: {row['extversion'] if row else 'MISSING'}")
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' ORDER BY table_name
        """)
        print("[db] tables: " + ", ".join(r["table_name"] for r in cur.fetchall()))
    print("[db] schema ready. Next: uv run python ingest.py")
