# E-commerce Order-Support Agent

A harness-controlled, tool-connected, RAG-grounded customer-support agent for a
fixed e-commerce domain, built from scratch for Assignment 1 (Harness, Tools &
Grounding). Full staged build plan and design rationale live in
[`docs/PLAN.md`](docs/PLAN.md).

## 1. What this is

A multi-turn support agent that handles order-status, delivery, refund, and
subscription/account tickets for one customer at a time. For each message it
classifies the ticket type, retrieves the relevant policy from a local vector
store, looks up that customer's own (mock) order and account data through
read-only tools, and answers — grounded in what it actually retrieved and looked
up, with short-term memory across the conversation and long-term memory against
the customer's prior tickets. The defining property: a **harness** (our code),
not the model, decides whether each proposed tool call is allowed to run, and it
scopes every call to the ticket's customer *before* dispatch — so the model can
only ever *propose*, and a cross-customer request never reaches a tool.

## 2. Prerequisites

- [uv](https://docs.astral.sh/uv/) — the project & dependency manager. Install once:
  - **Windows (PowerShell):** `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
  - **macOS / Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
- An API key for your chosen provider. The provider is selected by `LLM_PROVIDER`
  in `.env`: `groq` (free tier, the default here) needs `GROQ_API_KEY`; `anthropic`
  needs `ANTHROPIC_API_KEY`. Switching provider is a one-line `.env` change — no code edits.

uv manages the Python version and the virtual environment for you, so you do not
need to create or activate a venv by hand.

## 3. Setup

```bash
# 1. clone
git clone <your-repo-url>
cd ecommerce-agent

# 2. create the environment and install all dependencies (creates .venv/)
uv sync

# 3. add your API key(s)
cp .env.example .env        # Windows: copy .env.example .env
# then edit .env and fill in ANTHROPIC_API_KEY (and GROQ_API_KEY if using Groq)
```

Verify the setup with the provider smoke test. `uv run` executes inside the
project environment automatically — no manual activation needed:

```bash
uv run python llm.py        # should print a short model reply
```

<details>
<summary>Plain-pip fallback (no uv)</summary>

```bash
python -m venv .venv
source .venv/Scripts/activate   # Git Bash on Windows (forward slashes!)
# source .venv/bin/activate     # macOS / Linux
# .venv\Scripts\activate        # PowerShell / cmd
pip install -r requirements.txt
python llm.py
```
</details>

## 4. How to run

**Interactive conversation** (the main entry point). Holds a multi-turn chat as
the support agent; every proposed tool call passes `harness_check` first.

```bash
uv run python cli.py                                       # cust_1001, refund
uv run python cli.py --customer cust_2002 --type account   # different customer
```

**Demo scenarios** (the 7 scripted cases from `docs/PLAN.md` §8). Runs one, some,
or all end-to-end — the video demo walks through these.

```bash
uv run python scenarios.py               # run all seven
uv run python scenarios.py 1 6           # just scenarios 1 and 6
uv run python scenarios.py --list        # show the list
```

### Component-level demos (for isolation debugging)

Each stage's own demo remains runnable, useful when the full agent misbehaves
and you want to bisect which component is at fault.

**Harness boundary** (real MCP, no API key). Five proposed tool calls through
`harness_check`; three rejected before they reach MCP:
```bash
uv run python harness.py
```

**MCP tools directly** (bypassing the harness — the second layer). Malformed
call rejected by schema; cross-customer call rejected by server scope:
```bash
uv run python mcp_client.py
```

**RAG sanity check** (no LLM). Every covered question retrieves its expected
doc; the deliberately-uncovered questions return nothing:
```bash
uv run python retriever.py
```

**Memory** (no API key). Prior-ticket history and the buffer growing across
turns:
```bash
uv run python memory.py
```

## 5. Project structure

Flat module layout (mirrors the class samples; keeps imports friction-free on a
clean clone).

| Path | Purpose |
|---|---|
| `llm.py` | Provider abstraction (Anthropic / Groq) → normalized response |
| `docs/PLAN.md` | Staged build plan, data contracts, and design rationale |
| `docs/sync-harness-async-mcp.md` | Why the harness is sync and MCP calls are per-call |
| `requirements.txt` / `.env.example` / `.gitignore` | Setup and config |
| `ticket.py` | Active ticket / session context — the scoping + memory anchor |
| `harness.py` | The control loop + `harness_check` — **the boundary** |
| `mcp_server.py` / `mcp_client.py` | FastMCP stdio server + client for the two read-only tools |
| `mock_data.py` | Mock orders, accounts, prior-ticket history, and the ownership index |
| `tools_schema.py` | Tool specs advertised to the model |
| `policy_docs/` | 7 Markdown policy docs, one policy area each, with real numbers |
| `retriever.py` | Chroma + sentence-transformers RAG; traceable hits + honest-gap threshold |
| `memory.py` | Short-term conversation buffer + long-term history lookup |
| `agent.py` | Orchestration: classify → memory → RAG → harness loop, per turn |
| `cli.py` | Interactive conversation entry point |
| `scenarios.py` | The 7 scripted demo scenarios |

## 6. How one turn flows

```
Customer message
  -> short-term buffer (append)
  -> classify ticket type            (small LLM call)
  -> long-term memory lookup         (prior tickets by customer_id)
  -> RAG retrieve                    (top-k policy chunks + scores)
  -> threshold check -> inject       (chunks, or an explicit "no coverage" signal)
  -> LLM call: answer OR propose a tool call
       -> if tool proposed: harness_check()   <-- THE BOUNDARY
            allowed  -> MCP client -> scoped MCP server -> result -> back to LLM
            rejected -> reason returned to LLM, nothing dispatched
  -> answer appended to the buffer
```

The classify and answer steps are two separate LLM calls per turn; retrieval
runs between them. `harness_check()` (in `harness.py`) is the exact line where a
proposed tool call is allowed or rejected before anything runs.

## 7. Why I built the harness this way

The two justifications the assignment asks for (§3), with the full rationale in
[`docs/PLAN.md`](docs/PLAN.md) §4 and §11.

**How ticket types were scoped, and why.** The domain is fixed to e-commerce
order support, so ticket types are a closed set of four — order status, delivery,
refund, and subscription/account — matched 1:1 to the assignment's stated types.
A closed set gives the classifier a finite decision, makes routing and logging
explicit, and makes "did we handle this type" testable. Exactly one customer is
bound per conversation, which turns "does this ID belong to this ticket" into a
crisp, testable predicate and mirrors how real support tickets are scoped. A
customer's *other* tickets live in the prior-ticket history store and are reached
by lookup — never worked concurrently in the same chat.

**Where the permission boundary lives (the MCP-layer decision).** Permission is
enforced in two layers, primary at the harness. Before any proposed
`lookup_order` / `check_account_status` is dispatched, `harness_check()` resolves
the target's owner and rejects the call if it does not belong to the current
ticket's customer — so a cross-customer read never reaches the tool. The MCP
server is *additionally* launched scoped to that customer and re-checks every
call, so no unscoped path exists even if the server were called directly. This
keeps the harness the real boundary (avoiding the common pitfall of scoping
bolted inside the tool as the only gate) while guaranteeing the tool layer itself
cannot leak. The two layers are anchored to the *same* `customer_id` by
construction: it is passed into both `harness_check` and the client that launches
the scoped server.

## 8. Known limitations and next improvements

This is a Week-1 build; guardrails, an eval harness, and the one irreversible
action (`issue_refund`) are deliberately out of scope. Beyond those, two known
limitations worth flagging:

- **Single date field per order.** `OrderRecord` carries one `delivery_date`,
  with no distinction between the *estimated* date and the *actual delivered*
  date. The agent infers which is meant from the order `status` and reasons
  around it sensibly, but it cannot compute an exact "delayed by N days" for an
  order that has not been delivered yet. The fix is a two-field model
  (`estimated_delivery` + `delivered_on`), deferred to avoid a schema change
  rippling through the tool output, prompt, and scenarios late in the build.
- **Single-label classifier.** The classify step picks one ticket type; a
  genuinely mixed message (e.g. delivery *and* refund) is bucketed into one.
  Classification only drives routing and logging — it does not gate anything —
  so a mislabel is cosmetic, but a multi-label or confidence-scored classifier
  would be more faithful.
- **Groq tool-calling reliability.** The free-tier default (`openai/gpt-oss-120b`
  via Groq) is a slightly less reliable tool-caller than Claude. The provider
  seam (`llm.py`) makes switching to Anthropic a one-line `.env` change if
  needed.
