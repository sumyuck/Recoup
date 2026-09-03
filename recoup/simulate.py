"""The synthetic world: how customers and banks respond to what the agent does.

This is the honest core of the evaluation, so the causal model is written out
explicitly rather than buried:

    an order recovers  <=>  it self-healed  OR  some intervention landed

Both channels are live for treated orders. Only the first is live for the
holdout. That is precisely why a naive "we recovered 47% of failed payments"
claim is worthless -- a large slice of that 47% is the self-heal channel, which
would have fired with the agent switched off. The holdout measures it, and the
difference is the only number the agent can honestly claim.

Every draw is a deterministic function of (order_id, seed, action, attempt),
never of call order. Two consequences: a replay reproduces a run exactly, and
arm A cannot get luckier than arm C just because it ran first.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

from .models import ACTION_CHANNEL, Batch, Channel, GroundTruth, Intervention, Order

I = Intervention


def _u01(*parts) -> float:
    """Deterministic uniform(0,1) from a tuple of identifiers."""
    h = hashlib.sha256("\x00".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:13], 16) / float(1 << 52)


@dataclass
class Outcome:
    recovered: bool
    channel: str                      # "self_heal" | "intervention" | "none"
    side_effect: Optional[str] = None  # OPT_OUT | PROMISE_TO_PAY | NO_RESPONSE
    promised_for: Optional[datetime] = None
    detail: str = ""


class World:
    """Simulated customers and banks. Holds the ground truth; the agent never
    gets a reference to this object."""

    # Each additional unwanted contact raises the chance the customer opts out.
    # This is the mechanism that gives over-contacting a real, measured cost
    # instead of being free the way it is in most dunning demos.
    OPT_OUT_BASE = 0.012
    OPT_OUT_PER_CONTACT = 0.028
    # A customer who was going to pay anyway is more irritated by being chased.
    OPT_OUT_SELF_HEAL_MULT = 1.6

    def __init__(self, batch: Batch, seed: int = 20260903):
        self.gt: Dict[str, GroundTruth] = batch.ground_truth
        self.seed = seed
        self._contacts: Dict[str, int] = {}

    # -- the counterfactual channel -----------------------------------------
    def self_heals(self, order: Order) -> bool:
        """Would this order have recovered with the agent switched off?

        Drawn once per order and independent of anything the agent does, so it
        is identical in the treated and holdout arms. That is what makes the
        arms comparable.
        """
        g = self.gt[order.order_id]
        return _u01(order.order_id, self.seed, "self_heal") < g.self_heal_prob

    def self_heal_at(self, order: Order, from_time: datetime) -> Optional[datetime]:
        """WHEN a self-healing order would have recovered on its own.

        This exists because of a bug that flattered the agent. The first
        version resolved self-healers instantly, before the agent could act --
        so a customer who was always going to pay was never contacted, and the
        measured cost of over-contacting came out as exactly zero.

        Reality has no such courtesy: you cannot tell in advance which failures
        will heal, so you contact them and *then* they pay. Giving the self-heal
        channel a realistic arrival time makes the two channels compete on a
        timeline, which is the only way wasted contacts can be counted at all.

        Timing is class-dependent -- an outage clears in hours, a low balance
        waits for payday.
        """
        if not self.self_heals(order):
            return None
        g = self.gt[order.order_id]
        fast = {"ISSUER_DOWN", "GATEWAY_TIMEOUT", "UPI_COLLECT_EXPIRED"}
        if g.true_class.value in fast:
            lo, hi = 1.0, 14.0            # hours: the outage ends
        elif g.true_class.value in {"INSUFFICIENT_FUNDS", "MANDATE_INSUFFICIENT"}:
            lo, hi = 48.0, 22 * 24.0      # waits for salary credit
        else:
            lo, hi = 12.0, 10 * 24.0
        hours = lo + (hi - lo) * _u01(order.order_id, self.seed, "self_heal_at")
        return from_time + timedelta(hours=hours)

    # -- the intervention channel -------------------------------------------
    def act(
        self,
        order: Order,
        intervention: Intervention,
        attempt_idx: int,
        at: datetime,
    ) -> Outcome:
        g = self.gt[order.order_id]

        if g.unrecoverable:
            return Outcome(False, "none", detail="ground truth: unrecoverable")

        p = g.responsiveness.get(intervention.value, 0.0)

        # Timing matters, and the agent is rewarded for getting it right.
        # A scheduled retry that lands after the outage window, or on a payday,
        # genuinely works better than one fired blindly.
        if intervention in (I.RETRY_SCHEDULED, I.MANDATE_REPRESENT):
            if at.day in (1, 2, 3, 28, 29, 30, 31):
                p *= 1.25          # salary credited
            p = min(p, 0.95)

        # Repeat touches decay: the second SMS is worth much less than the first.
        p *= max(0.35, 1.0 - 0.22 * attempt_idx)

        hit = _u01(order.order_id, self.seed, intervention.value, attempt_idx) < p

        ch = ACTION_CHANNEL[intervention]
        side: Optional[str] = None
        promised: Optional[datetime] = None

        if ch in (Channel.SMS, Channel.WHATSAPP, Channel.VOICE, Channel.EMAIL):
            n = self._contacts.get(order.customer_id, 0)
            self._contacts[order.customer_id] = n + 1

            if not hit:
                # Annoyance -> permanent opt-out. Higher for people who were
                # going to pay anyway, which is what makes over-contacting
                # genuinely costly rather than merely wasteful.
                mult = self.OPT_OUT_SELF_HEAL_MULT if self.self_heals(order) else 1.0
                p_out = (self.OPT_OUT_BASE + self.OPT_OUT_PER_CONTACT * n) * mult
                if _u01(order.order_id, self.seed, "optout", attempt_idx) < p_out:
                    side = "OPT_OUT"
                else:
                    side = "NO_RESPONSE"

            # Voice and B2B email can yield a commitment instead of a payment.
            if not hit and side != "OPT_OUT" and intervention in (I.VOICE_CALL, I.NUDGE_WHATSAPP):
                if order.kind.value == "B2B_INVOICE" or order.amount_inr > 5000:
                    if _u01(order.order_id, self.seed, "promise", attempt_idx) < 0.30 * g.intent_strength * 2:
                        side = "PROMISE_TO_PAY"
                        promised = at + timedelta(days=int(3 + 6 * _u01(order.order_id, self.seed, "pdays")))

        return Outcome(
            recovered=hit,
            channel="intervention" if hit else "none",
            side_effect=side,
            promised_for=promised,
            detail=f"p={p:.3f} after timing and decay adjustments",
        )

    def honours_promise(self, order: Order) -> bool:
        """Not everyone who promises actually pays. Modelled off intent."""
        g = self.gt[order.order_id]
        return _u01(order.order_id, self.seed, "promise_kept") < (0.45 + 0.4 * g.intent_strength)
