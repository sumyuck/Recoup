# Recoup

**A bounded revenue-recovery agent that proves how much money it actually made.**

Razorpay AI Buildathon — Track 03, AI Revenue Recovery.

Recoup takes a batch of failed payments, failed subscription debits and overdue B2B
invoices; works out *why* each one failed; picks a recovery action; passes that action
through a policy gate that can refuse it; executes it against Razorpay test-mode APIs;
and records every decision in a hash-chained, replayable ledger.

Then it does the part most recovery demos skip: it measures itself against a
**randomized holdout that is never contacted**, and reports the difference.

---

## The one idea this project is built around

Almost any dunning system can report a big number:

> "We recovered 49% of failed payments."

That number is mostly not the system's work. In this corpus **23% of failed payments
recover on their own** — an issuer outage ends, a salary lands, a customer retries
unprompted. A system that touches everything gets to claim all of it.

So 20% of orders are deliberately assigned to a holdout and **never contacted at all**.
The only number Recoup claims is the difference:

| | value |
|---|---|
| Treated recovery rate | **49.2%** |
| Holdout recovery rate (agent off) | **19.0%** |
| **Incremental lift** | **+30.2pp** (95% CI +20.8 to +39.2) |
| **Incremental recovered** | **₹11,30,798** |
| Cost per ₹100 recovered | **₹0.285** |
| Lift over a naive retry schedule | **+19.2pp** / ₹8,72,528 |
| Share of the addressable ceiling captured | **62%** |

Full generated numbers: **[RESULTS.md](RESULTS.md)** — every figure is rendered from the
run that produced it, so the docs cannot drift from the code.

> **Current caveat, stated up front:** the committed run uses the offline **stub**
> diagnoser, so arm C is identical to arm B and the LLM's contribution is *not* yet
> measured. Set `ANTHROPIC_API_KEY` and run `make eval-live` to measure it. The stub
> establishes the floor the model has to beat: **78% accuracy on the ambiguous slice**
> that the deterministic tier cannot resolve.

---

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional: add ANTHROPIC_API_KEY and rzp_test_ keys
make demo                     # calibrate priors, run all arms, render results
make serve                    # dashboard on http://localhost:8000
```

No keys needed. Without them the pipeline runs fully offline: diagnosis uses the
deterministic stub and Razorpay calls run in shadow mode, logging the exact request
they would have sent.

```bash
make eval          # all three arms + report.json + RESULTS.md
make eval-live     # same, with the real model doing diagnosis
make chaos         # failure-path proof: no double charges under gateway failure
make verify        # verify the ledger hash chain
python cli.py trace order_<id>    # one order's full decision trail
```

---

## Architecture

```
  traffic (202k events)          at-risk orders (500)
          │                              │
          ▼                              ▼
  ┌───────────────┐              ┌───────────────────┐
  │  DETECTION    │              │  RISK REGISTER    │
  │  deterministic│─ incidents ─▶│  observable facts │
  │  two-proportion│             │  only, no labels  │
  │  z-test, both │              └─────────┬─────────┘
  │  cell geometries│                      │
  └───────────────┘                        ▼
                                 ┌───────────────────┐
                                 │  DIAGNOSIS        │  tier 1 lookup  (66%)
                                 │  tiered           │  tier 2 LLM     (34%)
                                 │  schema-validated │  tier 3 fallback
                                 └─────────┬─────────┘
                                           ▼
                                 ┌───────────────────┐
                                 │  PLAYBOOK         │  candidate actions
                                 │  + EV ranking     │  ranked by p·₹ − cost
                                 └─────────┬─────────┘
                                           ▼
                          ╔════════════════════════════════╗
                          ║  POLICY GATE   (policy.yaml)   ║  ◀── the LLM cannot
                          ║  13 rule families, allow/deny  ║      bypass this
                          ║  full trace on BOTH outcomes   ║
                          ╚════════════════┬═══════════════╝
                              allow │      │ deny → transient? human? terminal?
                                    ▼
                          ┌───────────────────────┐
                          │  BOUNDED EXECUTOR     │  idempotency keys
                          │  circuit breaker      │  reconcile on ambiguity
                          │  DLQ, bounded retries │  Razorpay test-mode
                          └───────────┬───────────┘
                                      ▼
                          ┌───────────────────────┐
                          │  HASH-CHAINED LEDGER  │  every decision, replayable
                          └───────────┬───────────┘
                                      ▼
                          ┌───────────────────────┐
                          │  EVALUATION           │  holdout · bootstrap CI
                          │                       │  ceiling · exception list
                          └───────────────────────┘
```

Design rationale and the decisions I rejected: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## What makes it *bounded*

[`policy.yaml`](policy.yaml) is the **entire** authorization surface for money and
contact actions. The LLM proposes; the policy engine authorizes. There is no override
path in code, so a hallucinating or prompt-injected model cannot do more than get
denied — and the denial is logged with the rule that caught it.

Enforced there, not in prose:

- **max 3 charge attempts** per order, exponential backoff with jitter
- **never retry** `RISK_BLOCKED`, `DO_NOT_HONOR`, `CARD_EXPIRED`, `MANDATE_REVOKED` —
  futile or actively harmful
- **contact caps** — 1 per 24h, 3 per 7d, min 18h between touches
- **quiet hours** 21:00–09:00 IST, **DND registry** honoured, **consent** required per channel
- **opt-out is permanent** across every channel and future batch
- **voice tier** gated on amount ≥ ₹5,000, two cheaper touches already failed,
  stored consent, and mandatory spoken AI disclosure
- **human approval** above ₹75,000, and always for `IN_DISPUTE` / `CHARGEBACK_OPEN` / `LEGAL_HOLD`
- **budget caps** per batch and per action, as a % of the amount at risk
- **stopping rules** — success, opt-out, promise-to-pay freeze, action ceiling,
  sequence age, and an **economic stop**: if EV < 1.5× cost, doing nothing is correct

Every verdict records the full rule trace on **allow as well as deny**, because "why was
this permitted?" is the more common audit question and logging only denials leaves it
unanswerable.

---

## The audit trail

`python cli.py trace order_<id>` reconstructs any order's history from the ledger.
A real example — a ₹39,130 overdue invoice:

```
DIAGNOSIS        INVOICE_OVERDUE (confidence 0.75, tier llm)
ACTION_PROPOSED  HUMAN_COLLECTIONS_CALL — believed 0.62, EV ₹24,260, cost ₹150
POLICY_VERDICT   ALLOWED
                   ✓ contact.opt_out_is_permanent    no opt-out on record
                   ✓ approval.always_escalate_flags  no blocking flags
                   ✓ approval.human_approval_above_inr  ₹39,130 within autonomous limit
                   ✓ stopping.min_expected_value_ratio  EV ₹24,260 vs cost ₹150
ACTION_EXECUTED  idem 8aa9566…  attempts 1
OUTCOME_OBSERVED not recovered
ACTION_PROPOSED  VOICE_CALL
POLICY_VERDICT   DENIED by contact.max_contacts_per_customer_per_24h
                   → transient; sequence deferred to 2026-09-02T11:05
ACTION_EXECUTED  VOICE_CALL_SCRIPT (hi-IN) spoke "unatalees hazaar ek sau tees rupees"
                   disclosure: ai_disclosure_present=True, credential_guard_stated=True
OUTCOME_OBSERVED RECOVERED via VOICE_CALL — ₹39,130
```

Each entry stores the hash of the previous one, so editing history invalidates every
hash after it and `make verify` reports the exact sequence number where the chain broke.

---

## Hinglish voice tier

Voice is the most expensive and most intrusive channel, so it is the most constrained.
Three things in [`recoup/channels/voice.py`](recoup/channels/voice.py) are load-bearing:

1. **AI disclosure is structural** — emitted as utterance one by the script builder
   itself. There is no flag to disable it, because an "internal testing" switch on a
   legally required disclosure is how those end up off in production.
2. **Never asks for credentials** — no card number, CVV, OTP, UPI PIN or password, and
   it says so out loud. Collection happens via a link sent after the call. A recovery
   bot that asks for an OTP is indistinguishable from the fraud it is recovering from.
3. **Amounts are spoken, not read** — `₹1,12,500` becomes
   *"ek lakh barah hazaar paanch sau rupees"*. Hindi numerals 21–99 are irregular words,
   not tens+ones compounds; my first version emitted "chalees paanch lakh" for 45 lakh,
   which is not Hindi.

---

## Honest limitations

The things I would attack first, and the places this would not survive contact with
production:

1. **The world is synthetic, and I wrote it.** Ground-truth responsiveness is my
   estimate of how customers behave. I mitigated the worst of it — the agent's action
   priors are *learned* from a separate calibration batch on a different seed
   (`scripts/calibrate.py`) rather than hand-typed, so the agent is not simply reading
   my answer key — but the causal structure is still mine. The measurement machinery
   is what transfers to real data; the specific rupee figure does not.
2. **I had to scale my own effectiveness estimates down by ~48%.** The first version
   produced +42pp lift and had the agent capturing 99% of the recovery ceiling. That is
   not a good result, it is a broken simulator; published dunning benchmarks put
   incremental lift in the 5–20pp band. `REALISM_SCALE` in `corpus.py` records the
   correction.
3. **Detection only works on cells with volume.** 1,456 cell-buckets were too sparse to
   test at all. Recall holds at 1.00 down to ~35% outage severity, then falls off a
   cliff to 0.10 at 15% — measured, in
   [`artifacts/detector_sensitivity_sweep.txt`](artifacts/detector_sensitivity_sweep.txt).
   A subtle, slow degradation would be missed entirely.
4. **`NO_ELIGIBLE_ACTION` is still the largest exception bucket** (~₹7.3L). Some is
   correct — risk-blocked orders, exhausted contact budgets — but not all of it, and I
   have not driven it down.
5. **The LLM's contribution is unmeasured in the committed run.** Stub mode makes arm C
   ≡ arm B. Until `make eval-live` runs, "the AI adds value" is a hypothesis with a
   floor (78% on the ambiguous slice), not a finding.
6. **Simulated customer responses, real Razorpay calls.** Payment links and orders are
   genuine test-mode API requests; whether a customer *pays* is simulated. Conversion
   numbers are not field-validated.
7. **No learning loop.** Priors are calibrated once, offline. A production system would
   update them continuously and would need guardrails against a feedback loop where the
   agent's own contact policy biases the data it learns from.
8. **Single-tenant, single-currency, no auth.** The dashboard is read-only by design,
   but it is unauthenticated.

---

## Layout

| path | what |
|---|---|
| `policy.yaml` | the entire authorization surface — read this first |
| `recoup/corpus.py` | synthetic corpus + hidden ground truth + injected outages |
| `recoup/detect.py` | deterministic detection, both cell geometries, Bonferroni |
| `recoup/diagnose.py` | tiered diagnosis, schema-validated, deterministic fallback |
| `recoup/policy.py` | the gate: 13 rule families, full trace, denial taxonomy |
| `recoup/executor.py` | idempotency, circuit breaker, reconciliation, DLQ |
| `recoup/orchestrator.py` | the recovery loop as a time-ordered event queue |
| `recoup/eval_harness.py` | holdout, bootstrap CI, ceiling, false-positive cost |
| `recoup/ledger.py` | append-only hash-chained ledger |
| `recoup/channels/voice.py` | Hinglish voice tier + compliance |
| `recoup/channels/razorpay_api.py` | Razorpay test-mode client (test-key enforced) |
| `scripts/calibrate.py` | learns action priors from a separate batch |
| `scripts/render_results.py` | regenerates RESULTS.md from report.json |
