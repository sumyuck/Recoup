#!/usr/bin/env python
"""Learn the agent's action priors from a calibration batch.

Why this exists: `policy.py` ships hand-written `DEFAULT_PRIORS`, and an agent
whose beliefs I typed in by hand -- while I also wrote the simulator's ground
truth -- proves nothing. It would be marking my own homework. So the priors the
agent actually runs on are estimated the way they would be in production: from
observed outcomes on historical attempts.

Method: a separate corpus on a different seed. Orders are split into a
**control** arm that gets no action at all and a **treatment** arm where each
order is assigned a RANDOM eligible intervention (exploration rather than
exploitation, so every arm of the playbook gets data). Outcomes are observed and
aggregated by (diagnosed class, intervention).

The control arm is what makes uplift estimable. Response and uplift are
different quantities and confusing them is the central error in dunning:

    response(class, action) = P(recover | action taken)
    uplift(class, action)   = P(recover | action) - P(recover | nothing done)

An ISSUER_DOWN failure recovers ~66% of the time on its own once the outage
clears. An agent ranking actions by *response* sees a 0.75 success rate there
and happily spends on it, when the action is worth almost nothing incrementally
-- and every one of those contacts risks an opt-out for recovery it was going
to get for free. Ranking by *uplift* points the budget at the orders where
intervening actually changes the outcome.

This is also the only objective consistent with how the system is scored: the
holdout measures incremental recovery, so the agent should optimise incremental
recovery.

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

import hashlib
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
    # Control arm: no action taken, so recovery here is purely organic.
    ctrl_n = defaultdict(int)
    ctrl_win = defaultdict(int)
    horizon_days = 21          # matches stopping.max_sequence_age_days

    def _is_control(order_id: str) -> bool:
        """Randomized control assignment, by hash.

        This was `i % 5 == 0` and that was a real bug. `batch.orders` is sorted
        by creation time, so taking every fifth element is *systematic*
        sampling, not randomization -- and it interacts with the injected outage
        windows, which are themselves time-localised. The control arm ended up
        with a different mix of ISSUER_DOWN orders than the treatment arm, which
        biased the per-class organic rate badly enough that measured uplift on
        transient classes came out NEGATIVE -- impossible, since recovery is
        (self-heal OR action).
        """
        h = hashlib.sha256(f"{order_id}:{CALIBRATION_SEED}:ctrl".encode()).hexdigest()
        return (int(h[:8], 16) % 100) < 20

    for i, o in enumerate(batch.orders):
        # Hold out a fifth of calibration orders as untouched controls.
        if _is_control(o.order_id):
            d0 = diag.diagnose(register[o.order_id])
            heal_at = world.self_heal_at(o, o.created_at)
            recovered = heal_at is not None and (heal_at - o.created_at).days <= horizon_days
            ctrl_n[d0.failure_class.value] += 1
            if recovered:
                ctrl_win[d0.failure_class.value] += 1
            continue

        d = diag.diagnose(register[o.order_id])
        cls = d.failure_class
        cands = PLAYBOOK.get(cls, [])
        if not cands:
            continue
        iv = rng.choice(cands)
        # NOTE: an earlier version skipped orders whose organic recovery had
        # already landed before the action time. That looked like hygiene and
        # was actually differential selection: fast-healing classes
        # (ISSUER_DOWN heals in 1-14h) had their quick self-healers dropped from
        # the TREATMENT arm while the control arm kept them. The result was a
        # measured uplift that was negative for every action on ISSUER_DOWN --
        # arithmetically impossible when recovery is (self-heal OR action), and
        # the tell that the two arms were no longer the same population.
        #
        # Both arms now score the same population on the same rule.
        at = o.created_at + timedelta(hours=6)
        # Both arms must be scored on the SAME outcome definition over the SAME
        # horizon, or uplift is garbage. My first version compared "did this one
        # action succeed at +6h" against "did the order recover organically any
        # time in 21 days", which biased uplift so far negative that transient
        # classes looked actively harmful to touch.
        #
        # The honest outcome for a treated order is: the action worked, OR it
        # recovered organically inside the horizon anyway.
        out_ = world.act(o, iv, 0, at)
        heal_at = world.self_heal_at(o, o.created_at)
        healed_in_window = (
            heal_at is not None and (heal_at - o.created_at).days <= horizon_days
        )
        trials[(cls.value, iv.value)] += 1
        if out_.recovered or healed_in_window:
            wins[(cls.value, iv.value)] += 1

    control_rates = {
        cls: round((ctrl_win[cls] + ALPHA) / (n + ALPHA + BETA), 4)
        for cls, n in ctrl_n.items()
    }

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
                "method": "random-action exploration with a 20% untouched control "
                          "arm; Laplace-smoothed success rates grouped by "
                          "(diagnosed class, intervention). Control arm gives the "
                          "organic recovery rate per class, so uplift = response - control.",
                "evaluation_seed_is_different": True,
                "control_horizon_days": horizon_days,
                "priors": dict(priors),
                "control_rates": control_rates,
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
    print(f"\n{'diagnosed class':24s} {'control':>8s}  organic recovery with NO action")
    print("-" * 52)
    for cls in sorted(control_rates):
        print(f"{cls:24s} {control_rates[cls]:>8.3f}  (n={ctrl_n[cls]})")

    print(f"\n{'class':24s} {'action':24s} {'resp':>6s} {'ctrl':>6s} {'UPLIFT':>7s}")
    print("-" * 72)
    last = None
    for cls, iv, n, w, p in rows:
        u = p - control_rates.get(cls, 0.0)
        c = cls if cls != last else ""
        last = cls
        flag = "  <- negative" if u <= 0 else ""
        print(f"{c:24s} {iv:24s} {p:>6.3f} {control_rates.get(cls,0):>6.3f} {u:>7.3f}{flag}")

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
