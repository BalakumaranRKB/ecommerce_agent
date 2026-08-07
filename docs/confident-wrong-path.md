# A confident wrong path

**Found:** Assignment 2, Stage 3 — by the trajectory eval, on its first run.
**Fixture:** `delivery_damaged` in `eval/fixtures.json`
**Artifact:** `eval/results/stage3-baseline.json` (git `fadf7157`, 11/12 = 91.7%)
**Reproduce:** `uv run python -m eval.reproduce_cwp` — deterministic, no LLM, seconds.

---

## The ticket

Customer `cust_4004`, delivery issue:

> The desk lamp from ord_8005 turned up smashed, what do I do?

## What the agent answered

> I'm sorry to hear the desk lamp arrived damaged. I checked ord_8005 — it shows as delivered on 2026-07-15.
>
> Unfortunately, I don't have a specific policy covering damaged-item claims, so I can't quote you exact terms or next steps from our knowledge base [NO RELEVANT POLICY FOUND]. I don't want to guess at a process that might not be accurate.
>
> I'll escalate this to a human agent who can arrange a replacement or refund for the damaged item. Is there anything else you'd like me to note for them (e.g., photos of the damage, preference for refund vs. replacement)?

## Why that reads as a good answer

Read it as a reviewer scoring answer quality. Nearly every signal is positive:

- **Specific.** Names the order, quotes the real delivery date, correctly identifies the problem.
- **Grounded.** Every factual claim it makes is true and came from a tool call.
- **Humble.** *"I don't want to guess at a process that might not be accurate."*
- **Transparent.** It even self-labels the gap: `[NO RELEVANT POLICY FOUND]`.
- **Actionable.** Escalates, and asks a sensible follow-up about photos and refund-vs-replacement.

This is what a well-behaved agent declining to hallucinate looks like. An
answer-quality rubric would likely score it *highly* — the hedging reads as good
judgement.

## What the trajectory shows

```
retrieval       retrieve  -> nothing
memory          prior_tickets  -> 1 prior ticket(s)
tool_call       lookup_order  {'order_id': 'ord_8005'}
```

Retrieval ran and returned nothing above the 0.32 similarity threshold.

**But the policy exists.** `policy_docs/damaged-or-missing-items.md` is in the
corpus, is embedded in `policy_chunks`, and is retrievable — just not by this
phrasing:

| Query | Top hit |
|---|---|
| "The item turned up smashed, what do I do?" — `retriever.py`'s own `COVERED_QUESTIONS` | `damaged-or-missing-items` **0.45** ✅ |
| "The **desk lamp from ord_8005** turned up smashed, what do I do?" — this ticket | **nothing above 0.32** ❌ |

Same question. Adding the order id and the product name — exactly what a real
customer writes — pushed the correct document below the threshold entirely.

So the agent's statement *"I don't have a specific policy covering damaged-item
claims"* is **false**. The customer was told to wait for a human when the policy
could have answered them immediately.

## Why final-answer checking would have missed this

The answer contains no hallucination, no factual error, and no bad reasoning.
Every claim it makes is true *given what it was handed*. A checker comparing the
response to the retrieved context would find perfect agreement — because the
retrieved context was empty, and the agent faithfully said so.

The sharper version, and the reason this case is worth the writeup:

> **A well-calibrated agent will faithfully report a broken pipeline, and that
> report will look like competence.** The hedging is what convinces a reader the
> answer is trustworthy. Answer-quality grading does not merely miss this failure
> — it actively rewards it.

The failure is entirely upstream of the text and completely invisible in it. The
only place it is visible is the trajectory, where `retrieve -> nothing` sits
directly beside a corpus that contains the answer.

## Why the existing sanity check missed it

`retriever.py` ships a `COVERED_QUESTIONS` list asserting every policy document
is retrievable. It passes 7/7. It always has.

The problem is who wrote those questions: the same person who wrote the policy
documents, in the same vocabulary, at the same sitting. They are clean, abstract
policy questions — *"The item turned up smashed."* Real tickets are messy, and
carry order ids, product names, and narrative. MiniLM averages all of those
tokens into a single vector, and the extra tokens pull it away from the document
it should match.

A fixed list of self-authored sanity questions is **structurally incapable** of
catching this. It confirms the corpus is reachable in principle; it says nothing
about whether it is reachable in practice.

## This is systemic, not a one-off

Three instances of the same root cause, found across three different stages,
none of them deliberately hunted:

| # | Found at | Query | Retrieved | Correct doc |
|---|---|---|---|---|
| 1 | Stage 1 | "My order ord_5001 hasn't arrived and it's well past the date I was promised" | `return-eligibility` (0.42), `damaged-or-missing-items` (0.41) | `shipping-delay-compensation` — missing |
| 2 | Stage 2 | "can you tell me about the delivery status of ord_5001" | `damaged-or-missing-items` (0.36), `return-eligibility` (0.33) | `shipping-delay-compensation` — missing |
| 3 | **Stage 3** | "The desk lamp from ord_8005 turned up smashed, what do I do?" | **nothing** | `damaged-or-missing-items` — missing |

Instances 1 and 2 are arguably worse in one respect: they retrieved *wrong*
documents rather than none, so the agent had irrelevant policy in context while
claiming no relevant policy existed.

Instance 3 is the headline case here because it is the only one preserved as a
complete artifact — query, trajectory, answer text, and git SHA — rather than as
a screenshot.

## How the eval catches it

The fixture asserts on the trajectory, never on the answer:

```json
{
  "id": "delivery_damaged",
  "customer_id": "cust_4004",
  "message": "The desk lamp from ord_8005 turned up smashed, what do I do?",
  "requires": {
    "retrieval": true,
    "expect_doc_id": "damaged-or-missing-items"
  }
}
```

`expect_doc_id` is the rule that matters. Asserting merely that *retrieval
happened* would pass this case — retrieval did happen, it just found nothing.
Naming the document that must come back is what turns "the agent looked
something up" into "the agent looked up the right thing."

Failure output:

```
delivery_damaged  FAIL
  ! expected doc 'damaged-or-missing-items' not retrieved (got: nothing)
```

This fixture stays in the suite permanently. Once the fix lands it becomes a
regression guard: if retrieval ever becomes brittle to phrasing again, this is
the test that goes red.

## The fix

**Status: not yet applied.** Documented before fixing, deliberately — §2.2
requires the eval to fail this case first, and it currently does.

**Hypothesis:** order ids and product names dilute the query embedding. Stripping
them before embedding — while the tool call still receives the untouched message
— should recover the correct document.

Verify with:

```bash
uv run python -m eval.reproduce_cwp --hypothesis
```

That re-runs each failing query with specifics removed and reports how many
recover. If none do, the hypothesis is wrong and the cause lies elsewhere.

**Alternatives considered:**

- *Lower `MIN_SIMILARITY`.* Rejected as a first move: the threshold was tuned
  against a real leak boundary (weakest passing hit 0.37, strongest leaked hit
  0.30), and instance 3 retrieved *nothing* — dropping the floor far enough to
  catch it would very likely start leaking documents into the two honest-gap
  questions, trading a false negative for a false positive.
- *Rewrite the policy documents to include customer phrasings.* Helps, but scales
  badly — it means anticipating vocabulary rather than fixing the mechanism.
- *Reword the fixture to match the sanity-check phrasing.* Rejected outright.
  That is tuning the test until it passes, which is the precise anti-pattern this
  assignment exists to teach against. The fixture's phrasing is realistic; the
  retriever is brittle.

Once applied, all three instances above should pass, and the before/after will be
measurable against `eval/results/stage3-baseline.json`.
