"""Budget governor -- deciding *which* actions a fixed batch budget should fund.

The problem this solves is the largest single weakness the evaluation exposed.
With a hard ₹5,000 batch cap, the agent was spending it first-come-first-served
in simulated-time order: whatever order the event queue reached first got
funded, and once the cap was hit, **227 later candidate actions were refused**
regardless of how valuable they were. A ₹96,000 invoice arriving late in the
batch lost to a ₹400 order that happened to arrive early.

That is not a budget problem, it is an allocation problem. A fixed budget over
a known set of opportunities with per-item cost and expected payoff is a
knapsack, and the right answer is to fund by **expected value density** --
expected rupees recovered per rupee spent -- not by arrival time.

How it works
------------
After the diagnosis pre-pass, every treated order's best paid candidate is
projected. Those are sorted by density and funded greedily until the budget is
exhausted. The density of the last affordable item is the budget's **shadow
price** (λ): the expected return the marginal rupee is earning. During the run,
a paid action is funded only if its own density clears λ.

Free actions -- gateway retries -- never consume budget and are never gated
here. They are always worth trying.

Projecting demand correctly
---------------------------
My first version projected each order's *single highest-density* paid candidate
and it was inert: density is EV/cost, so the highest-density action is almost
always the cheapest one, and the projection came out at ₹26 of demand against a
₹5,000 budget that the run then actually exhausted. It concluded the budget was
never binding and rationed nothing.

The error was ignoring that recovery is a **ladder**. An order does not take one
action; it tries a ₹0.05 link, fails, escalates to a ₹0.80 WhatsApp, fails,
escalates to an ₹18 call. The expensive rungs are what consume the budget, and
they only get reached when the cheap ones fail.

So every paid rung of every order is projected, each weighted by the probability
of actually *reaching* it -- the product of the preceding rungs failing:

    reach(k) = Π_{j<k} (1 - p_j)          expected_cost(k) = cost_k · reach(k)

Demand is the sum of expected costs. Rationing is still by per-action density,
because that is what the gate can evaluate at decision time.

Remaining approximations, stated because they matter
-----------------------------------------------------
*   Reach probabilities assume rungs are tried in playbook order and are
    independent. Real sequences are gated by consent, quiet hours and caps, so
    some rungs are never reachable for some customers -- demand is slightly
    overestimated, which biases λ high. Rationing too hard is the safer error.
*   Projections use the agent's own priors, which are estimates. A wrong prior
    misprices an action. The governor is only as good as the priors it is
    handed -- which is why those are calibrated from data rather than typed.
*   λ is computed once per batch. A production system facing continuous arrivals
    would re-solve periodically as the remaining budget and the arrival
    distribution shift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Projection:
    order_id: str
    intervention: str
    amount_inr: float
    believed_p: float
    cost_inr: float
    # Probability this rung is actually reached, i.e. every cheaper rung before
    # it failed. Without this, expensive escalations look far more likely than
    # they are and demand is wildly overstated.
    reach_prob: float = 1.0

    @property
    def ev_inr(self) -> float:
        """Expected recovery *conditional on taking this action*."""
        return self.believed_p * self.amount_inr

    @property
    def expected_cost_inr(self) -> float:
        """Cost weighted by the chance of getting this far down the ladder."""
        return self.cost_inr * self.reach_prob

    @property
    def density(self) -> float:
        """Expected rupees recovered per rupee spent, if this action is taken."""
        return (self.ev_inr / self.cost_inr) if self.cost_inr > 0 else float("inf")


@dataclass
class BudgetGovernor:
    budget_inr: float
    enabled: bool = True
    safety_factor: float = 1.0
    # Set by plan()
    shadow_price: float = 0.0
    planned_spend: float = 0.0
    planned_actions: int = 0
    projected_demand: float = 0.0
    projected_candidates: int = 0
    admitted: int = 0
    refused: int = 0
    refused_value_inr: float = 0.0

    def plan(self, projections: List[Projection]) -> Dict:
        """Greedy knapsack by density; returns the resulting plan."""
        paid = [p for p in projections if p.cost_inr > 0]
        self.projected_candidates = len(paid)
        self.projected_demand = round(sum(p.expected_cost_inr for p in paid), 2)

        if not self.enabled or not paid:
            self.shadow_price = 0.0
            return self._plan_dict(reason="governor disabled" if not self.enabled else "no paid candidates")

        # If everything fits, budget is not binding and nothing should be gated.
        if self.projected_demand <= self.budget_inr:
            self.shadow_price = 0.0
            return self._plan_dict(reason="projected demand fits inside the budget; "
                                          "no rationing needed")

        # Greedy by density, spending the budget on the best returns first.
        # Expected cost is what accumulates, since that is what the batch will
        # actually pay out across the ladder.
        ranked = sorted(paid, key=lambda p: -p.density)
        spend = 0.0
        cutoff = 0.0
        n = 0
        for p in ranked:
            if spend + p.expected_cost_inr > self.budget_inr:
                break
            spend += p.expected_cost_inr
            cutoff = p.density
            n += 1

        self.shadow_price = cutoff * self.safety_factor
        self.planned_spend = round(spend, 2)
        self.planned_actions = n
        return self._plan_dict(reason="budget is binding; rationing by expected-value density")

    def _plan_dict(self, reason: str) -> Dict:
        return {
            "enabled": self.enabled,
            "budget_inr": self.budget_inr,
            "projected_paid_candidates": self.projected_candidates,
            "projected_demand_inr": self.projected_demand,
            "planned_spend_inr": self.planned_spend,
            "planned_actions": self.planned_actions,
            "shadow_price": round(self.shadow_price, 3),
            "safety_factor": self.safety_factor,
            "reason": reason,
            "interpretation": (
                f"fund a paid action only if it expects to return at least "
                f"₹{self.shadow_price:.2f} per ₹1 spent"
                if self.shadow_price > 0 else
                "no density threshold applied"
            ),
        }

    def admits(self, ev_inr: float, cost_inr: float, amount_inr: float) -> Tuple[bool, str]:
        """Should this paid action be funded?"""
        if cost_inr <= 0:
            return True, "free action; does not consume batch budget"
        if not self.enabled or self.shadow_price <= 0:
            self.admitted += 1
            return True, "budget not binding"
        density = ev_inr / cost_inr
        if density >= self.shadow_price:
            self.admitted += 1
            return True, (f"EV density {density:.1f}x clears the budget shadow price "
                          f"{self.shadow_price:.1f}x")
        self.refused += 1
        self.refused_value_inr += amount_inr
        return False, (f"EV density {density:.1f}x is below the budget shadow price "
                       f"{self.shadow_price:.1f}x; the same rupee buys more elsewhere "
                       f"in this batch")

    def stats(self) -> Dict:
        return {
            "enabled": self.enabled,
            "shadow_price": round(self.shadow_price, 3),
            "planned_spend_inr": self.planned_spend,
            "projected_demand_inr": self.projected_demand,
            "admitted": self.admitted,
            "refused_below_threshold": self.refused,
            "refused_value_inr": round(self.refused_value_inr, 2),
        }
