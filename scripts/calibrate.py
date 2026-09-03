#!/usr/bin/env python
"""Learn the agent's action priors from a calibration batch.

Why this exists: `policy.py` ships hand-written `DEFAULT_PRIORS`, and an agent
whose beliefs I typed in by hand -- while I also wrote the simulator's ground
truth -- proves nothing. It would be marking my own homework. So the priors the
agent actually runs on are estimated the way they would be in production: from
observed outcomes on historical attempts.

Method: a separate corpus on a different seed, each order assigned a RANDOM
eligible intervention (exploration rather than exploitation, so every arm of
the playbook gets data), outcomes observed, then aggregated by
(diagnosed class, intervention).

Two deliberate choices:

*   Grouped by **diagnosed** class, not true class. At calibration time you
    only know what your own diagnoser said, so estimating against ground truth
    would leak information the live agent will never have.
*   **Laplace smoothing** on the estimate, so a 1-for-1 cell does not become a
    100% prior and dominate the expected-value ranking forever.

The evaluation seed and the calibration seed are different on purpose. Priors
fitted on the same batch they are then scored against would be overfitted, and
the lift number would be quietly inflated.
"""
from __future__ import annotations

import json
import os
import random
import sys
from collections import defaultdict
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recoup.corpus import generate_batch          # noqa: E402
from recoup.detect import build_risk_register, detect_degradations  # noqa: E402
from recoup.diagnose import Diagnoser             # noqa: E402
from recoup.policy import PLAYBOOK                # noqa: E402
from recoup.simulate import World                 # noqa: E402

CALIBRATION_SEED = 424242          # deliberately NOT the evaluation seed
ALPHA, BETA = 1.0, 1.0             # Laplace prior


def main(n_orders: int = 4000, out: str = "artifacts/priors.json") -> None:
    print(f"Calibrating action priors on {n_orders} orders (seed {CALIBRATION_SEED})")
    print("Exploration policy: one random eligible action per order.\n")

    batch = generate_batch(n_orders=n_orders, seed=CALIBRATION_SEED, holdout_pct=0)
    world = World(batch, seed=CALIBRATION_SEED)
    signals = detect_degradations(batch.traffic)
    register = build_risk_register(batch, signals)
    diag = Diagnoser(mode="stub")      # deterministic tier only, no API needed
    rng = random.Random(CALIBRATION_SEED)

    trials = defaultdict(int)
    wins = defaultdict(int)

    for o in batch.orders:
        d = diag.diagnose(register[o.order_id])
        cls = d.failure_class
        cands = PLAYBOOK.get(cls, [])
        if not cands:
            continue
        iv = rng.choice(cands)
        # Only count attempts where the organic recovery had not already landed,
        # otherwise a self-healer credits whatever action happened to be tried.
        heal_at = world.self_heal_at(o, o.created_at)
        at = o.created_at + timedelta(hours=6)
        if heal_at is not None and heal_at <= at:
            continue
        out_ = world.act(o, iv, 0, at)
        trials[(cls.value, iv.value)] += 1
        if out_.recovered:
            wins[(cls.value, iv.value)] += 1

    priors = defaultdict(dict)
    rows = []
    for (cls, iv), n in sorted(trials.items()):
        w = wins[(cls, iv)]
        p = (w + ALPHA) / (n + ALPHA + BETA)
        priors[cls][iv] = round(p, 4)
        rows.append((cls, iv, n, w, p))

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as fh:
        json.dump(
            {
                "calibration_batch": f"seed={CALIBRATION_SEED}, n={n_orders}",
                "method": "random-action exploration; Laplace-smoothed success rate "
                          "grouped by (diagnosed class, intervention)",
                "evaluation_seed_is_different": True,
                "priors": dict(priors),
            },
            fh,
            indent=2,
        )

    print(f"{'diagnosed class':24s} {'intervention':24s} {'n':>5s} {'wins':>5s} {'prior':>7s}")
    print("-" * 70)
    last = None
    for cls, iv, n, w, p in rows:
        c = cls if cls != last else ""
        last = cls
        print(f"{c:24s} {iv:24s} {n:>5d} {w:>5d} {p:>7.3f}")
    print(f"\n{len(rows)} (class, action) cells estimated -> {out}")
    thin = [r for r in rows if r[2] < 20]
    if thin:
        print(f"WARNING: {len(thin)} cells have fewer than 20 trials; their priors are "
              f"dominated by smoothing and should not be trusted:")
        for cls, iv, n, w, p in thin[:8]:
            print(f"   {cls}/{iv}: n={n}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    main(n)
