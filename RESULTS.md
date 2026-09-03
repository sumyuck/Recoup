# Results

> Generated from `artifacts/report.json` on 2026-09-03T23:06:49.
> Reproduce: `python cli.py eval --orders 500 --seed 20260903 --live`

- **Corpus** — 500 at-risk orders, 360 customers, 202,005 traffic events, ₹3,760,982 at risk, seed `20260903`
- **Diagnosis mode** — `live` (`claude-sonnet-5`)
- **Policy** — v3; priors learned from seed=424242, n=4000
- **Ledger hash chain** — valid

## The recovery ceiling

A recovery rate without a denominator is theatre. Of the money at risk:

| pool | orders | value | note |
|---|---:|---:|---|
| Total at risk | 500 | ₹3,760,982 | |
| Structurally unrecoverable | 30 | ₹377,227 | risk-blocked, uncollectable B2B |
| Would self-heal anyway | 116 | ₹766,021 | recovers with the agent switched off |
| **Addressable** | **354** | **₹2,617,734** | the only pool the agent can claim |

## Arms

All arms share the same corpus, executor, policy gate and simulated world. Only the
decision policy differs.

| arm | treated recovery | holdout recovery | lift | 95% CI | incremental | cost | net | ₹/₹100 | significant |
|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| `A_BASELINE` | 27.93% (112/401) | 23.23% (23/99) | **+4.7pp** | -4.6 – +13.2 | ₹331,617 | ₹0 | ₹331,617 | 0.000 | **no** |
| `B_RULES` | 42.14% (169/401) | 23.23% (23/99) | **+18.9pp** | +9.4 – +28.0 | ₹962,656 | ₹5,000 | ₹957,656 | 0.519 | yes |
| `C_AGENT` | 47.63% (191/401) | 23.23% (23/99) | **+24.4pp** | +14.8 – +33.4 | ₹1,030,972 | ₹5,108 | ₹1,025,864 | 0.495 | yes |

**Against the retry schedule a merchant already runs** (arm A, not the holdout): `C_AGENT` adds **+19.7pp** and **₹699,355** for ₹5,108 of spend — 0.730 rupees per ₹100 recovered. That is the number a merchant actually buys.

## False-positive cost

What a raw recovery rate hides.

| arm | self-heal recoveries *not* claimed | wasted contacts | opt-outs caused | parked for human |
|---|---:|---:|---:|---:|
| `A_BASELINE` | 77 | 0 | 0 | 0 |
| `B_RULES` | 81 | 9 | 17 | 10 |
| `C_AGENT` | 79 | 6 | 16 | 10 |

## Detection

| metric | value |
|---|---:|
| outages injected | 2 |
| incidents reported | 2 |
| precision / recall | 1.0 / 1.0 |
| hypotheses tested | 3563 |
| α (Bonferroni-corrected) | 2.81e-06 (naive 0.01) |
| candidates before correction | 13 |
| cross-dimension shadows collapsed | 3 |
| cell-buckets too sparse to test | 1447 |

Sensitivity is measured, not assumed — see [`artifacts/detector_sensitivity_sweep.txt`](artifacts/detector_sensitivity_sweep.txt). Recall holds at 1.00 down to ~35% outage severity and then degrades to 0.10 at 15%.

## Diagnosis

Overall accuracy **0.9969** on n=327, split by tier because a blended figure hides whether the model earns its share of traffic:

| tier | n | accuracy |
|---|---:|---:|
| `deterministic` | 144 | 1.0 |
| `llm` | 183 | 0.9945 |

Accuracy **per arm**, on the same rows — this is the mechanism behind the
agent's recovery advantage, and leaving it out of an earlier version of this
report made arm C's lift look unexplained:

| arm | n | diagnosis accuracy |
|---|---:|---:|
| `B_RULES` | 327 | 0.6911 |
| `C_AGENT` | 327 | 0.9969 |

Routing: 144 resolved by lookup, 183 sent to the model, 0 fell back (0 schema violations, 0 API errors). Model cost ₹108.

## Executor invariants

| invariant | value |
|---|---:|
| `calls` | 569 |
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
| `NO_ELIGIBLE_ACTION` | 184 | ₹1,192,882 |
| `HUMAN_ESCALATED` | 10 | ₹395,590 |
| `OPT_OUT` | 16 | ₹32,616 |

Plus 10 orders parked for human approval (above the autonomous limit, or flagged in dispute / legal hold).

## Ablation — does the model earn its place?

The most useful experiment in the project. Same seed, same policy, same
executor; the only difference is how messy the error fields are.

On a **clean** corpus every failure is fully determined by its structured
`error_reason`, so a dict lookup resolves everything and the model is dead
weight. On a **noisy** corpus — 35% of orders with dropped, vendor-specific or
misattributed fields, cause still recoverable from the free-text description —
the lookup table degrades and the model does not.

| | clean corpus | noisy corpus (default) |
|---|---:|---:|
| deterministic tier resolves | 207 orders | 144 orders |
| routed to the model | 123 orders | 183 orders |
| diagnosis accuracy | 1.0 | 0.9969 |
| rules-only lift | +22.4pp | +18.9pp |
| agent lift | +25.6pp | +24.4pp |
| **model contribution (C − B)** | **+3.3pp / ₹23,708** | **+5.5pp / ₹68,316** |

So the answer is conditional, and worth stating plainly: **on tidy data the
model is not worth its latency or its cost. It earns its place precisely where
the structured fields stop being trustworthy** — which is what production data
looks like.

