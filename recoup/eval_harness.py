"""Evaluation harness -- the part that decides whether any of this worked.

The headline number is **incremental** recovery against a randomized holdout,
not raw recovery rate. That distinction is the whole point:

    raw recovery        "we recovered 61% of failed payments"
    incremental         "we recovered 36 percentage points MORE than the
                         orders we deliberately left alone"

The first number is what a dunning demo reports. It is also mostly fiction: in
this corpus roughly a quarter of failed payments recover on their own -- an
outage ends, a salary lands, a customer retries unprompted -- so any system
that touches everything can claim a 25% "recovery" while doing nothing of
value. The holdout is the only way to separate the agent's contribution from
the world's, and it is why 20% of orders are deliberately never contacted.

Everything else here exists to stop a good headline hiding a bad system:

*   **Bootstrap confidence intervals**, because a point estimate of lift on 400
    orders is not a fact, it is a sample. If the interval crosses zero, the
    result is noise and the report says so.
*   **Cost per Rs100 recovered**, because recovery bought with unlimited
    spend is not recovery, it is a transfer.
*   **False-positive cost**: contacts sent to customers who would have paid
    anyway, and the opt-outs that resulted. Over-contacting is the failure mode
    a dunning system is most likely to hide.
*   **Recovery ceiling**, so the number has a denominator that means something.
    Capturing 71% of what was ever recoverable is a different claim from
    recovering 61% of everything.
*   **An honest exception list**: what it could not recover, and why.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .models import Batch
from .orchestrator import RunResult
from .simulate import World


def _rate(recovered: int, n: int) -> float:
    return (recovered / n) if n else 0.0


@dataclass
class ArmMetrics:
    arm: str
    n_treated: int
    n_holdout: int
    treated_recovered: int
    holdout_recovered: int
    treated_value_at_risk: float
    holdout_value_at_risk: float
    treated_value_recovered: float
    holdout_value_recovered: float
    spend_inr: float
    llm_cost_inr: float
    actions_taken: int
    contacts_made: int
    # --- honesty metrics ---
    self_heal_credited: int          # recoveries the agent must NOT claim
    wasted_contacts: int             # contacts to orders that self-healed anyway
    opt_outs_caused: int
    human_escalations: int
    # --- derived, filled by compute() ---
    lift_pp: float = 0.0
    lift_ci_low: float = 0.0
    lift_ci_high: float = 0.0
    incremental_orders: float = 0.0
    incremental_inr: float = 0.0
    incremental_inr_ci_low: float = 0.0
    incremental_inr_ci_high: float = 0.0
    net_value_inr: float = 0.0
    cost_per_100_recovered: Optional[float] = None
    pct_of_ceiling: Optional[float] = None
    significant: bool = False

    def to_dict(self) -> Dict:
        return {
            "arm": self.arm,
            "treated": {
                "n": self.n_treated,
                "recovered": self.treated_recovered,
                "recovery_rate_pct": round(_rate(self.treated_recovered, self.n_treated) * 100, 2),
                "value_at_risk_inr": round(self.treated_value_at_risk, 2),
                "value_recovered_inr": round(self.treated_value_recovered, 2),
            },
            "holdout": {
                "n": self.n_holdout,
                "recovered": self.holdout_recovered,
                "recovery_rate_pct": round(_rate(self.holdout_recovered, self.n_holdout) * 100, 2),
                "value_at_risk_inr": round(self.holdout_value_at_risk, 2),
                "value_recovered_inr": round(self.holdout_value_recovered, 2),
                "note": "never contacted; measures what recovers with the agent off",
            },
            "incremental": {
                "lift_pp": round(self.lift_pp, 2),
                "lift_95ci_pp": [round(self.lift_ci_low, 2), round(self.lift_ci_high, 2)],
                "statistically_significant": self.significant,
                "orders": round(self.incremental_orders, 1),
                "inr": round(self.incremental_inr, 2),
                "inr_95ci": [round(self.incremental_inr_ci_low, 2), round(self.incremental_inr_ci_high, 2)],
                "pct_of_recovery_ceiling": self.pct_of_ceiling,
            },
            "economics": {
                "spend_inr": round(self.spend_inr, 2),
                "llm_cost_inr": round(self.llm_cost_inr, 4),
                "total_cost_inr": round(self.spend_inr + self.llm_cost_inr, 2),
                "net_value_inr": round(self.net_value_inr, 2),
                "cost_per_100_recovered_inr": self.cost_per_100_recovered,
                "actions_taken": self.actions_taken,
                "contacts_made": self.contacts_made,
            },
            "false_positive_cost": {
                "self_heal_recoveries_not_claimed": self.self_heal_credited,
                "wasted_contacts_to_self_healers": self.wasted_contacts,
                "opt_outs_caused": self.opt_outs_caused,
                "note": "customers contacted who would have paid anyway, and the "
                        "permanent opt-outs that resulted. This is the cost a "
                        "raw recovery rate hides.",
            },
            "human_review_queue": self.human_escalations,
        }


def _bootstrap_lift(
    treated: List[Tuple[bool, float]],
    holdout: List[Tuple[bool, float]],
    iters: int = 2000,
    seed: int = 20260903,
) -> Dict:
    """Percentile bootstrap CI for the lift, in both rate and rupee terms.

    Resampling both arms with replacement is the right tool here: recovery is a
    Bernoulli outcome weighted by a heavily skewed amount distribution, so a
    normal-approximation interval on the rupee figure would be badly wrong in
    the tail. The bootstrap makes no distributional assumption.
    """
    rng = random.Random(seed)
    nt, nh = len(treated), len(holdout)
    if nt == 0 or nh == 0:
        return {"lift_pp": (0.0, 0.0), "inr": (0.0, 0.0)}

    lifts: List[float] = []
    incs: List[float] = []
    t_total_value = sum(a for _, a in treated)
    for _ in range(iters):
        ts = [treated[rng.randrange(nt)] for _ in range(nt)]
        hs = [holdout[rng.randrange(nh)] for _ in range(nh)]
        tr = sum(1 for r, _ in ts if r) / nt
        hr = sum(1 for r, _ in hs if r) / nh
        lifts.append((tr - hr) * 100)
        # Value-weighted: share of at-risk value recovered, differenced.
        tv = sum(a for r, a in ts if r) / max(1e-9, sum(a for _, a in ts))
        hv = sum(a for r, a in hs if r) / max(1e-9, sum(a for _, a in hs))
        incs.append((tv - hv) * t_total_value)

    lifts.sort()
    incs.sort()
    lo, hi = int(0.025 * iters), int(0.975 * iters) - 1
    return {"lift_pp": (lifts[lo], lifts[hi]), "inr": (incs[lo], incs[hi])}


def compute_arm_metrics(
    batch: Batch,
    result: RunResult,
    world: World,
    ceiling_value: Optional[float] = None,
) -> ArmMetrics:
    treated = [result.states[i] for i in result.treated_ids]
    holdout = [result.states[i] for i in result.holdout_ids]

    t_pairs = [(s.recovered, s.order.amount_inr) for s in treated]
    h_pairs = [(s.recovered, s.order.amount_inr) for s in holdout]

    t_rec = sum(1 for s in treated if s.recovered)
    h_rec = sum(1 for s in holdout if s.recovered)
    t_val_risk = sum(s.order.amount_inr for s in treated)
    h_val_risk = sum(s.order.amount_inr for s in holdout)
    t_val_rec = sum(s.order.amount_inr for s in treated if s.recovered)
    h_val_rec = sum(s.order.amount_inr for s in holdout if s.recovered)

    # Recoveries that arrived through the self-heal channel. The agent does not
    # get to count these, and naming them explicitly is the point.
    self_heal = sum(1 for s in treated if s.recovered_via == "self_heal")
    # Contacts spent on customers who were going to pay regardless. Counted
    # only for orders whose organic recovery actually arrived, so this is
    # money and goodwill genuinely burned, not a hypothetical.
    wasted = sum(s.contacts_made for s in treated if s.recovered_via == "self_heal")
    opt_outs = result.ledger.count_opt_outs() if hasattr(result.ledger, "count_opt_outs") else sum(
        1 for e in result.ledger.entries if e["event"] == "OPT_OUT"
    )

    llm_cost = float(result.diagnoser_stats.get("llm_cost_inr", 0.0) or 0.0)

    m = ArmMetrics(
        arm=result.arm,
        n_treated=len(treated),
        n_holdout=len(holdout),
        treated_recovered=t_rec,
        holdout_recovered=h_rec,
        treated_value_at_risk=t_val_risk,
        holdout_value_at_risk=h_val_risk,
        treated_value_recovered=t_val_rec,
        holdout_value_recovered=h_val_rec,
        spend_inr=result.policy.spend_inr,
        llm_cost_inr=llm_cost,
        actions_taken=sum(len(s.actions) for s in treated),
        contacts_made=sum(s.contacts_made for s in treated),
        self_heal_credited=self_heal,
        wasted_contacts=wasted,
        opt_outs_caused=opt_outs,
        human_escalations=len(result.human_queue),
    )

    t_rate, h_rate = _rate(t_rec, len(treated)), _rate(h_rec, len(holdout))
    m.lift_pp = (t_rate - h_rate) * 100
    m.incremental_orders = (t_rate - h_rate) * len(treated)

    t_vrate = t_val_rec / t_val_risk if t_val_risk else 0.0
    h_vrate = h_val_rec / h_val_risk if h_val_risk else 0.0
    m.incremental_inr = (t_vrate - h_vrate) * t_val_risk

    ci = _bootstrap_lift(t_pairs, h_pairs)
    m.lift_ci_low, m.lift_ci_high = ci["lift_pp"]
    m.incremental_inr_ci_low, m.incremental_inr_ci_high = ci["inr"]
    # "Significant" here means the 95% interval excludes zero. Stated as a
    # boolean so a null result cannot be quietly presented as a win.
    m.significant = m.lift_ci_low > 0

    total_cost = m.spend_inr + m.llm_cost_inr
    m.net_value_inr = m.incremental_inr - total_cost
    if m.incremental_inr > 0:
        m.cost_per_100_recovered = round(total_cost / (m.incremental_inr / 100.0), 4)
    if ceiling_value and ceiling_value > 0:
        m.pct_of_ceiling = round(m.incremental_inr / ceiling_value * 100, 2)

    return m


def recovery_ceiling(batch: Batch, world: World) -> Dict:
    """How much was ever recoverable, and how much of that needed the agent.

    Without this the recovery number has no meaningful denominator. Some orders
    are structurally dead (risk-blocked, no responsive channel) and counting
    them against the agent is as dishonest as claiming the self-healers.
    """
    total = 0.0
    unrecoverable = 0.0
    self_heal_value = 0.0
    addressable = 0.0     # not dead, and would NOT have self-healed
    n_unrec = n_self = n_addr = 0

    for o in batch.orders:
        g = batch.ground_truth[o.order_id]
        total += o.amount_inr
        if g.unrecoverable or not g.responsiveness:
            unrecoverable += o.amount_inr
            n_unrec += 1
            continue
        if world.self_heals(o):
            self_heal_value += o.amount_inr
            n_self += 1
            continue
        addressable += o.amount_inr
        n_addr += 1

    treated_share = sum(1 for o in batch.orders if not o.is_holdout) / max(1, len(batch.orders))
    return {
        "total_at_risk_inr": round(total, 2),
        "structurally_unrecoverable_inr": round(unrecoverable, 2),
        "structurally_unrecoverable_orders": n_unrec,
        "would_self_heal_inr": round(self_heal_value, 2),
        "would_self_heal_orders": n_self,
        "addressable_inr": round(addressable, 2),
        "addressable_orders": n_addr,
        # The ceiling the treated arm could in principle capture.
        "treated_addressable_inr": round(addressable * treated_share, 2),
        "note": "ceiling = value that was neither structurally dead nor going to "
                "recover on its own. This is the only pool the agent can claim.",
    }


def render_report(
    batch: Batch,
    metrics: List[ArmMetrics],
    ceiling: Dict,
    detection: Dict,
    diagnosis: Dict,
    executor_stats: Dict,
    exceptions: Dict,
    meta: Dict,
) -> Dict:
    return {
        "meta": meta,
        "corpus": {
            "orders": len(batch.orders),
            "customers": len(batch.customers),
            "traffic_events": len(batch.traffic),
            "total_at_risk_inr": batch.total_at_risk_inr,
            "seed": batch.seed,
        },
        "recovery_ceiling": ceiling,
        "arms": [m.to_dict() for m in metrics],
        "detection": detection,
        "diagnosis": diagnosis,
        "executor_invariants": executor_stats,
        "exceptions": exceptions,
    }


def build_exception_list(result: RunResult, batch: Batch, limit: int = 25) -> Dict:
    """What the agent could not recover, grouped by why.

    Ranked by rupees so the biggest unresolved items are visible rather than
    averaged away. A cherry-picked success table with no exception list is a
    demo, not a result.
    """
    from collections import defaultdict

    buckets: Dict[str, List[Dict]] = defaultdict(list)
    for oid in result.treated_ids:
        st = result.states[oid]
        if st.recovered:
            continue
        reason = st.stop_reason or "EXHAUSTED_LADDER"
        d = result.diagnoses.get(oid)
        buckets[reason].append(
            {
                "order_id": oid,
                "amount_inr": st.order.amount_inr,
                "diagnosed_class": d.failure_class.value if d else None,
                "true_class": batch.ground_truth[oid].true_class.value,
                "actions_tried": st.actions,
                "blocked_by": st.blocked_by,
            }
        )

    summary = {}
    for reason, items in sorted(buckets.items(), key=lambda kv: -sum(i["amount_inr"] for i in kv[1])):
        items.sort(key=lambda i: -i["amount_inr"])
        summary[reason] = {
            "orders": len(items),
            "value_inr": round(sum(i["amount_inr"] for i in items), 2),
            "largest": items[:5],
        }
    return {
        "unrecovered_by_reason": summary,
        "human_review_queue": sorted(
            result.human_queue, key=lambda h: -h["amount_inr"]
        )[:limit],
    }
