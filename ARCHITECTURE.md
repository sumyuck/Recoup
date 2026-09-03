# Architecture & decision log

What I chose, what I rejected, and what I got wrong and had to fix. The bugs are
included on purpose — they are the part of the build that shows the reasoning.

---

## 1. The LLM proposes; a deterministic gate authorizes

**Decision.** The model never executes anything. It produces a diagnosis; a playbook
turns that into candidate actions; [`policy.yaml`](policy.yaml) decides which are
allowed. There is no code path from a model output to a side effect that skips the gate.

**Rejected: letting the model choose the action directly** (tool-calling into the
executor). It demos better and is a worse system. Two reasons that matter for money:

- A prompt-injected or simply confused model cannot exceed what the rules already
  permit. The worst case is a denial, and the denial is logged with the rule that
  caught it.
- "Why did we charge this customer three times?" needs an answer that is a list of rule
  evaluations, not a model's post-hoc account of its own reasoning.

**Consequence I accepted:** the agent can only do what the playbook contemplates. It
cannot invent a novel recovery strategy. For money movement that is the right trade.

---

## 2. Statistics in code, judgement in the model

Detection is a two-proportion z-test over counts. Diagnosis is an LLM.

**Rejected: asking the model whether a success rate dropped.** It is arithmetic — exact,
cheap, instant, and reproducible. Routing it through a model adds latency and cost and
makes the numbers non-deterministic, which would poison every downstream measurement.

The model's turn comes where the input is unstructured and the answer is genuinely
contested: mapping messy `error_reason` + free text + outage context onto a cause, and
deciding whether `insufficient_funds` during a gateway incident is really the customer's
balance or the bank misreporting. Different answer, different recovery action.

**Tiering falls out of this.** Most failures carry an unambiguous `error_reason`;
`invalid_card_expiry` means the card is dead and there is nothing to interpret. So a
lookup table handles ~66% of volume at zero cost, and the model gets the ~34% that is
actually hard. Accuracy is reported per tier, so the model has to earn its slice —
a blended "91% accurate" would hide the deterministic tier's 100% carrying the average.

---

## 3. Holdout, not raw recovery rate

The single most consequential decision. 20% of orders are randomly assigned to a
holdout and never contacted.

**Why it is not optional.** 23% of failed payments in this corpus recover on their own.
Any system that touches everything can report that as its own work. The holdout is the
only way to separate the agent's contribution from the world's.

**Stratified, not a coin flip.** My first version flipped a per-order coin. B2B invoices
are ~30× the value of a typical order, so an unstratified holdout could land 6 or 16 of
them by luck and the headline rupee figure would swing by lakhs on sampling noise. The
holdout is now stratified by `(order kind × true failure class)` — which also balances
self-heal rate across arms, the variable that most directly biases lift — with orders
ranked inside each stratum by a stable hash so assignment is deterministic across
replays.

**Bootstrap CIs, not point estimates.** Recovery is Bernoulli weighted by a heavily
skewed amount distribution, so a normal approximation on the rupee figure would be badly
wrong in the tail. 2,000-iteration percentile bootstrap, and if the interval crosses
zero the report says **not significant** rather than quoting the point estimate.

**Two lift numbers, not one.** Lift vs the holdout answers *does this work at all*. Lift
vs arm A — the naive retry schedule a merchant already runs — answers *what am I
buying*. The second is smaller and is the honest commercial number, so both are
reported.

---

## 4. Time-ordered event queue, not a per-order loop

Contact frequency caps are defined **per customer across orders**. Processing each
order's sequence to completion before starting the next would let a customer with three
failed orders be messaged three times in a minute while every individual sequence looked
compliant. A single global priority queue keyed on simulated time makes the caps
actually bind.

---

## 5. Idempotency as the protected invariant

Every money action is keyed on `(order_id, intervention, attempt_idx)`, checked before
the call and recorded after.

**The branch that matters** is an ambiguous gateway timeout. A timeout means *unknown*,
not *failed*. Treating it as failed and retrying is exactly how a customer gets debited
twice, so an ambiguous result triggers a reconciliation sweep to establish true state
before anything else happens on that order.

`make chaos` runs 0/15/35/60% injected failure. At 60%: 68 transient errors, 36
ambiguous timeouts all reconciled, 2 breaker trips, 25 dead-lettered — and recovery
moves by one order. Double charges stay 0.

The chaos table alone proves nothing about idempotency, because the orchestrator never
happens to resubmit an identical action — `dup_prevented` sits at 0. Real duplicates
arrive from outside: a retried webhook, a double-clicked button, a replayed queue
message. So there is an explicit duplicate-submission proof: three identical
submissions, one gateway call, two duplicates prevented, identical outcomes returned.

---

## 6. Hash-chained JSONL, not a database

Each entry carries the previous entry's hash, and its own hash covers that link. Editing
any historical entry invalidates every hash after it, and `verify()` reports the exact
sequence number where the chain broke.

**Rejected: SQLite as source of truth.** The whole run is a pure function of the ledger
file, which is what makes "why did we call this customer at 10:14 on the 2nd?"
answerable months later by replay. An audit log you can silently `UPDATE` is not
evidence. Decision *inputs* are snapshotted rather than referenced, because looking up an
order later tells you what it looks like now, not what the agent saw when it decided.

---

## 7. Priors are learned, not typed

`policy.py` ships hand-written fallback priors, but the agent runs on values estimated by
`scripts/calibrate.py`: a separate corpus on a **different seed**, one random eligible
action per order (exploration, so every playbook arm gets data), outcomes observed, then
Laplace-smoothed success rates grouped by **diagnosed** class — not true class, because
at calibration time you only know what your own diagnoser said.

I did this because I wrote both the agent's beliefs *and* the simulator's ground truth,
and hand-typing the priors would have been marking my own homework. The learned values
disagree with my estimates in useful places: I had `MANDATE_REPRESENT` at 0.54, the data
says 0.26.

---

## Bugs I hit, and what each one cost

These are in the log because each one changed the result.

| # | bug | how it showed up | fix |
|---|---|---|---|
| 1 | Detection found **nothing** | 96 cells × 6.4k events = 0.2 attempts per cell-hour; no bucket cleared a volume floor | realistic traffic volume (202k events), 3-hour buckets |
| 2 | Issuer outages invisible | sliced only one way; an issuer outage is diluted across every gateway | test **both** cell geometries independently |
| 3 | Precision 0.4 | 2 injected outages surfaced as 5 "incidents" | cross-dimension **attribution**: one root cause, largest effect size wins, rest recorded as shadows |
| 4 | ~30 phantom incidents | thousands of hypotheses at α=0.01 | Bonferroni correction; α → 2.8e-6 |
| 5 | Probe denials invisible | 195 orders logged `NO_ELIGIBLE_ACTION` with no reason — the trail could answer "why did we act?" but not "why didn't we?" | return the full candidate probe trace and log it |
| 6 | Transient denials treated as **permanent** | 85 orders blocked by a 24h contact cap were abandoned forever | denial taxonomy: TRANSIENT / HUMAN / TERMINAL, with a computed re-wake time |
| 7 | 51 orders needing human review **vanished** | `always_escalate_flags` fired in the probe, so the real gate never ran and no escalation event was emitted | explicit human queue; disputed/legal-hold orders are parked, never dropped |
| 8 | Voice tier could never fire | `human_approval_above_inr: 25000` sat below the B2B invoice range, so nearly every receivable was parked | threshold → ₹75,000, documented in `policy.yaml` |
| 9 | Budget halt tripped while *thinking* | probing an expensive voice call set `halted=True` and killed the rest of the batch | `dry_run` probes cannot mutate engine state |
| 10 | ~19% of corpus diagnosed `UNKNOWN` | abandoned checkouts and overdue invoices have **no payment attempt**, so error fields read off `latest_attempt` were all `None` | derive facts from observable order state; diagnosis accuracy 0.74 → 0.92 |
| 11 | False-positive cost was **structurally 0** | self-healers resolved at step 0, before the agent could contact them, so wasted contacts could never be counted | `self_heal_at` — organic recovery competes on the timeline; lift fell 35.9 → 34.4pp |
| 12 | +42pp lift, **99% of ceiling** | my effectiveness estimates were fantasy | `REALISM_SCALE = 0.52`, uncollectable B2B segment; lift → 30.2pp |
| 13 | Hindi numerals wrong | composed 45 as "chalees paanch"; 21–99 are irregular words | full 0–99 table; 45 → "paintaalees" |
| 14 | `HUMAN_ESCALATION` ambiguous | the *action* (a person calls) collided with the policy *stop* (parked for approval) | action renamed `HUMAN_COLLECTIONS_CALL` |
| 15 | ~1,500 Razorpay 429s "fixed" with backoff | it was not throttling — test mode caps payment links at **30 per account, permanently** | eval shadows Razorpay by default; a dedicated proof script demonstrates the live leg |
| 16 | Idempotency keys collided across arms | key had no run scope, so arm C's writes hit arm B's `reference_id` upstream | run-scoped `reference_id`; the collision itself became the upstream-idempotency proof |
| 17 | Reported a 429 as proof of idempotency | the proof script checked `status >= 400`, so a throttle read as duplicate-rejection | check the error *code*; report "proves nothing" when it is a throttle |
| 18 | LLM scored a suspicious 1.00 | easy B2B rows pooled with genuinely degraded ones | split accuracy by difficulty; the honest figure is 0.988 on 82 degraded rows |

Bugs 11 and 12 both made the headline number **worse**, which is the point of building
the measurement rig before tuning the agent.

---

## Findings from the live Razorpay integration

Two came out of running against a real test account, and both changed the design.

**Test mode caps payment links at 30 per account, forever.** Not a rate limit — a
permanent quota. The first live run consumed all 30 and then produced ~1,500 HTTP 429s
that I read as throttling and tried to fix with backoff. The real message was
`test mode limit of 30 reached for payment_link`. The correct response was not a better
retry policy but an architectural one: the evaluation shadows Razorpay by default and a
separate `scripts/razorpay_live_proof.py` proves the leg is wired. A measurement run must
never depend on somebody else's quota, or its runtime and results vary with their limiter.

**Upstream idempotency is inconsistent across endpoints.** `payment_links.reference_id`
is enforced unique; `orders.receipt` is not, by default. I discovered the first because
arms B and C generated identical idempotency keys — the key is
`sha256(order_id, intervention, attempt_idx)` with no run scope — and Razorpay rejected
the collisions with `already exists`. That was the API confirming the protection is real
upstream. Then I checked whether Orders behaved the same way and found they do not:
two POSTs with an identical `receipt` returned two distinct order ids.

The conclusion is the one the design already assumed but had not verified: the local
hash-chained idempotency ledger is the load-bearing guard, and API-side uniqueness is an
inconsistent secondary net. `reference_id` is now run-scoped so a replay of the same seed
does not collide with itself.

## What I would build next

1. **Measure the LLM tier for real.** Stub mode makes arm C ≡ arm B. The floor is 78% on
   the ambiguous slice; until a live run beats it, "AI adds value" is a hypothesis.
2. **Drive down `NO_ELIGIBLE_ACTION`** — the largest exception bucket at ~₹7.3L.
3. **Sequential testing** instead of a fixed 20% holdout, so the experiment can stop
   early once lift is established and stop burning recoverable money on a control group.
4. **Contextual bandit over the playbook**, replacing static priors — with an explicit
   guard against the feedback loop where the agent's own contact policy biases the data
   it learns from.
5. **Real webhook ingestion** (`payment.failed`, `subscription.charged`) instead of a
   generated corpus, and a reconciliation sweep against Razorpay settlement reports.
