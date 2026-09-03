# Results

> Generated from `artifacts/report.json` on 2026-09-03T15:14:03.
> Reproduce: `python cli.py eval --orders 500 --seed 20260903`

- **Corpus** — 500 at-risk orders, 360 customers, 202,031 traffic events, ₹2,944,948 at risk, seed `20260903`
- **Diagnosis mode** — `stub`
- **Policy** — v3; priors learned from seed=424242, n=4000
- **Ledger hash chain** — valid

> ⚠️ **This run used the offline stub diagnoser.** Arm C is therefore identical to
> arm B by construction, and the LLM's contribution is *not* measured here.
> Headline numbers should only be quoted from a `--live` run.

## The recovery ceiling

A recovery rate without a denominator is theatre. Of the money at risk:

| pool | orders | value | note |
|---|---:|---:|---|
| Total at risk | 500 | ₹2,944,948 | |
| Structurally unrecoverable | 25 | ₹388,256 | risk-blocked, uncollectable B2B |
| Would self-heal anyway | 115 | ₹458,939 | recovers with the agent switched off |
| **Addressable** | **360** | **₹2,097,754** | the only pool the agent can claim |

## Arms

All arms share the same corpus, executor, policy gate and simulated world. Only the
decision policy differs.

| arm | treated recovery | holdout recovery | lift | 95% CI | incremental | cost | net | ₹/₹100 | significant |
|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| `A_BASELINE` | 30.0% (120/400) | 19.0% (19/100) | **+11.0pp** | +1.8 – +19.5 | ₹258,270 | ₹0 | ₹258,270 | 0.000 | yes |
| `B_RULES` | 49.25% (197/400) | 19.0% (19/100) | **+30.2pp** | +20.8 – +39.2 | ₹1,130,798 | ₹3,220 | ₹1,127,577 | 0.285 | yes |
| `C_AGENT` | 49.25% (197/400) | 19.0% (19/100) | **+30.2pp** | +20.8 – +39.2 | ₹1,130,798 | ₹3,220 | ₹1,127,577 | 0.285 | yes |

**Against the retry schedule a merchant already runs** (arm A, not the holdout): `C_AGENT` adds **+19.2pp** and **₹872,528** for ₹3,220 of spend — 0.369 rupees per ₹100 recovered. That is the number a merchant actually buys.

## False-positive cost

What a raw recovery rate hides.

| arm | self-heal recoveries *not* claimed | wasted contacts | opt-outs caused | parked for human |
|---|---:|---:|---:|---:|
| `A_BASELINE` | 78 | 0 | 0 | 0 |
| `B_RULES` | 79 | 3 | 18 | 15 |
| `C_AGENT` | 79 | 3 | 18 | 15 |

## Detection

| metric | value |
|---|---:|
| outages injected | 2 |
| incidents reported | 2 |
| precision / recall | 1.0 / 1.0 |
| hypotheses tested | 3565 |
| α (Bonferroni-corrected) | 2.81e-06 (naive 0.01) |
| candidates before correction | 14 |
| cross-dimension shadows collapsed | 3 |
| cell-buckets too sparse to test | 1456 |

Sensitivity is measured, not assumed — see [`artifacts/detector_sensitivity_sweep.txt`](artifacts/detector_sensitivity_sweep.txt). Recall holds at 1.00 down to ~35% outage severity and then degrades to 0.10 at 15%.

## Diagnosis

Overall accuracy **0.9108** on n=325, split by tier because a blended figure hides whether the model earns its share of traffic:

| tier | n | accuracy |
|---|---:|---:|
| `deterministic` | 199 | 1.0 |
| `llm` | 126 | 0.7698 |

Routing: 199 resolved by lookup, 126 sent to the model, 0 fell back (0 schema violations, 0 API errors). Model cost ₹0.

## Executor invariants

| invariant | value |
|---|---:|
| `calls` | 522 |
| `transient_errors` | 0 |
| `ambiguous_timeouts` | 0 |
| `retries` | 0 |
| `breaker_trips` | 0 |
| `breaker_shed` | 0 |
| `reconciliations` | 0 |
| `dead_lettered` | 0 |
| `duplicate_charges_prevented` | 0 |
| `double_charges` | 0 ⟵ **must be 0** |

Verified under injected failure at 0/15/35/60% chaos via `python cli.py chaos`, including an explicit duplicate-submission proof.

## Exception list — what it could not recover

| reason | orders | value |
|---|---:|---:|
| `NO_ELIGIBLE_ACTION` | 149 | ₹526,681 |
| `EXHAUSTED_LADDER` | 21 | ₹210,257 |
| `HUMAN_ESCALATED` | 15 | ₹144,532 |
| `OPT_OUT` | 18 | ₹67,784 |

Plus 15 orders parked for human approval (above the autonomous limit, or flagged in dispute / legal hold).

