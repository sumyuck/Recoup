"""Policy engine -- the deterministic gate between intent and action.

The central design decision in Recoup: **the LLM proposes, the policy engine
authorizes.** Nothing that spends money or contacts a human goes out without
passing every rule in `policy.yaml`, and there is no override path in code.

Two consequences worth stating plainly:

*   A prompt-injected or hallucinating model cannot cause harm beyond what the
    rules already permit. The worst it can do is propose something that gets
    denied -- and the denial is logged with the rule that caught it.
*   Every action has an answer to "why was this allowed?" that is a list of
    rule evaluations, not a model's self-report of its own reasoning.

`evaluate()` returns the FULL rule trace on both allow and deny. Logging only
denials would leave the far more common question -- why did this one go
through? -- unanswerable.
"""
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import yaml

from .models import (
    ACTION_CHANNEL,
    ACTION_COST_INR,
    MONEY_ACTIONS,
    Channel,
    CustomerFlag,
    CustomerView,
    FailureClass,
    Intervention,
    Order,
)

I = Intervention


# ---------------------------------------------------------------------------
# Denial taxonomy. A rule can refuse an action for one of three reasons, and
# conflating them was a real bug: the first version treated every refusal as
# final, so an order blocked by a customer's 24-hour contact cap was abandoned
# permanently instead of being picked up the next day. 85 orders in a 500-order
# batch died that way, and the ledger recorded them as "no eligible action" --
# technically true, materially wrong.
#
#   TRANSIENT -- the constraint clears with time; come back later
#   HUMAN     -- a person must decide; park it in the review queue, never drop
#   TERMINAL  -- genuinely nothing more to do
# ---------------------------------------------------------------------------
TRANSIENT_DENIALS = {
    # Rationing is not permanent: an order refused for low EV density stays
    # eligible, because budget frees up as cheaper orders resolve.
    "budget.below_shadow_price",
    "contact.max_contacts_per_customer_per_24h",
    "contact.max_contacts_per_customer_per_7d",
    "contact.min_hours_between_contacts",
    "contact.quiet_hours_ist",
    "stopping.promise_to_pay_pauses_sequence",
}

HUMAN_DENIALS = {
    "approval.always_escalate_flags",
    "approval.human_approval_above_inr",
}


def classify_denial(rule: Optional[str]) -> str:
    if rule in HUMAN_DENIALS:
        return "HUMAN"
    if rule in TRANSIENT_DENIALS:
        return "TRANSIENT"
    return "TERMINAL"


# ---------------------------------------------------------------------------
# The playbook: which interventions are even candidates for a given root cause,
# in default preference order. This encodes payments domain knowledge, and the
# omissions are as deliberate as the inclusions -- there is no RETRY on
# CARD_EXPIRED because a dead card cannot be charged, so offering it as a
# candidate would just burn a gateway call and an attempt from the cap.
# ---------------------------------------------------------------------------
PLAYBOOK: Dict[FailureClass, List[Intervention]] = {
    FailureClass.ISSUER_DOWN: [I.RETRY_SCHEDULED, I.METHOD_SWITCH_LINK, I.NUDGE_WHATSAPP, I.NUDGE_SMS],
    FailureClass.GATEWAY_TIMEOUT: [I.RETRY_NOW, I.RETRY_SCHEDULED, I.NUDGE_SMS],
    FailureClass.INSUFFICIENT_FUNDS: [I.RETRY_SCHEDULED, I.NUDGE_WHATSAPP, I.METHOD_SWITCH_LINK, I.NUDGE_SMS, I.VOICE_CALL],
    FailureClass.AUTH_3DS_TIMEOUT: [I.METHOD_SWITCH_LINK, I.NUDGE_WHATSAPP, I.NUDGE_SMS, I.NUDGE_EMAIL],
    FailureClass.CARD_EXPIRED: [I.UPDATE_INSTRUMENT_LINK, I.NUDGE_WHATSAPP, I.NUDGE_SMS, I.VOICE_CALL],
    FailureClass.DO_NOT_HONOR: [I.METHOD_SWITCH_LINK, I.NUDGE_WHATSAPP, I.VOICE_CALL],
    FailureClass.UPI_COLLECT_EXPIRED: [I.RETRY_NOW, I.NUDGE_WHATSAPP, I.NUDGE_SMS],
    FailureClass.MANDATE_REVOKED: [I.MANDATE_REAUTH_LINK, I.NUDGE_WHATSAPP, I.VOICE_CALL],
    FailureClass.MANDATE_INSUFFICIENT: [I.MANDATE_REPRESENT, I.NUDGE_WHATSAPP, I.NUDGE_SMS],
    FailureClass.RISK_BLOCKED: [],           # intentionally empty: hands off
    FailureClass.CHECKOUT_ABANDONED: [I.NUDGE_WHATSAPP, I.METHOD_SWITCH_LINK, I.NUDGE_SMS, I.NUDGE_EMAIL],
    FailureClass.INVOICE_OVERDUE: [I.NUDGE_EMAIL, I.NUDGE_WHATSAPP, I.VOICE_CALL, I.HUMAN_COLLECTIONS_CALL],
    FailureClass.UNKNOWN: [I.NUDGE_EMAIL],   # cheapest, least intrusive probe
}


# Fallback priors, used only if artifacts/priors.json is absent. These are the
# agent's BELIEF about what works -- deliberately not the simulator's ground
# truth, because an agent that already knows the answer proves nothing. The
# real ones are learned from a calibration batch (scripts/calibrate.py).
DEFAULT_PRIORS: Dict[str, Dict[str, float]] = {
    FailureClass.ISSUER_DOWN.value: {I.RETRY_SCHEDULED.value: 0.70, I.METHOD_SWITCH_LINK.value: 0.55, I.NUDGE_WHATSAPP.value: 0.35, I.NUDGE_SMS.value: 0.30, I.RETRY_NOW.value: 0.25},
    FailureClass.GATEWAY_TIMEOUT.value: {I.RETRY_NOW.value: 0.68, I.RETRY_SCHEDULED.value: 0.62, I.NUDGE_SMS.value: 0.30},
    FailureClass.INSUFFICIENT_FUNDS.value: {I.RETRY_SCHEDULED.value: 0.48, I.NUDGE_WHATSAPP.value: 0.40, I.METHOD_SWITCH_LINK.value: 0.42, I.NUDGE_SMS.value: 0.34, I.VOICE_CALL.value: 0.52, I.RETRY_NOW.value: 0.12},
    FailureClass.AUTH_3DS_TIMEOUT.value: {I.METHOD_SWITCH_LINK.value: 0.56, I.NUDGE_WHATSAPP.value: 0.48, I.NUDGE_SMS.value: 0.40, I.NUDGE_EMAIL.value: 0.24},
    FailureClass.CARD_EXPIRED.value: {I.UPDATE_INSTRUMENT_LINK.value: 0.54, I.NUDGE_WHATSAPP.value: 0.44, I.NUDGE_SMS.value: 0.36, I.VOICE_CALL.value: 0.50},
    FailureClass.DO_NOT_HONOR.value: {I.METHOD_SWITCH_LINK.value: 0.34, I.NUDGE_WHATSAPP.value: 0.20, I.VOICE_CALL.value: 0.26},
    FailureClass.UPI_COLLECT_EXPIRED.value: {I.RETRY_NOW.value: 0.58, I.NUDGE_WHATSAPP.value: 0.54, I.NUDGE_SMS.value: 0.44},
    FailureClass.MANDATE_REVOKED.value: {I.MANDATE_REAUTH_LINK.value: 0.44, I.NUDGE_WHATSAPP.value: 0.34, I.VOICE_CALL.value: 0.46},
    FailureClass.MANDATE_INSUFFICIENT.value: {I.MANDATE_REPRESENT.value: 0.54, I.NUDGE_WHATSAPP.value: 0.38, I.NUDGE_SMS.value: 0.32},
    FailureClass.RISK_BLOCKED.value: {},
    FailureClass.CHECKOUT_ABANDONED.value: {I.NUDGE_WHATSAPP.value: 0.44, I.METHOD_SWITCH_LINK.value: 0.38, I.NUDGE_SMS.value: 0.34, I.NUDGE_EMAIL.value: 0.26},
    FailureClass.INVOICE_OVERDUE.value: {I.NUDGE_EMAIL.value: 0.30, I.NUDGE_WHATSAPP.value: 0.40, I.VOICE_CALL.value: 0.58, I.HUMAN_COLLECTIONS_CALL.value: 0.68},
    FailureClass.UNKNOWN.value: {I.NUDGE_EMAIL.value: 0.15},
}


@dataclass
class Proposal:
    order_id: str
    intervention: Intervention
    rationale: str
    believed_success_prob: float
    scheduled_for: Optional[datetime] = None
    incentive_inr: float = 0.0

    @property
    def cost_inr(self) -> float:
        return ACTION_COST_INR[self.intervention] + self.incentive_inr


@dataclass
class RuleResult:
    rule: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> Dict:
        return {"rule": self.rule, "passed": self.passed, "detail": self.detail}


@dataclass
class Verdict:
    allowed: bool
    proposal: Optional[Proposal]
    rules: List[RuleResult] = field(default_factory=list)
    denial_rule: Optional[str] = None
    denial_detail: str = ""
    requires_human: bool = False
    stop_sequence: bool = False
    stop_reason: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "allowed": self.allowed,
            "intervention": self.proposal.intervention.value if self.proposal else None,
            "believed_success_prob": round(self.proposal.believed_success_prob, 4) if self.proposal else None,
            "cost_inr": round(self.proposal.cost_inr, 4) if self.proposal else None,
            "scheduled_for": self.proposal.scheduled_for.isoformat() if self.proposal and self.proposal.scheduled_for else None,
            "denial_rule": self.denial_rule,
            "denial_detail": self.denial_detail,
            "requires_human": self.requires_human,
            "stop_sequence": self.stop_sequence,
            "stop_reason": self.stop_reason,
            # The full trace, allow or deny. This is the audit answer to
            # "why was this permitted?", not just "why was it blocked?".
            "rules_evaluated": [r.to_dict() for r in self.rules],
        }


class PolicyEngine:
    def __init__(self, policy_path: str, priors_path: Optional[str] = None,
                 governor=None):
        with open(policy_path) as fh:
            self.cfg = yaml.safe_load(fh)
        self.version = int(self.cfg["policy_version"])
        self.priors = DEFAULT_PRIORS
        self.priors_source = "built-in defaults"
        if priors_path and os.path.exists(priors_path):
            with open(priors_path) as fh:
                loaded = json.load(fh)
            self.priors = loaded.get("priors", DEFAULT_PRIORS)
            self.priors_source = f"learned from {loaded.get('calibration_batch', priors_path)}"

        # --- mutable run state ------------------------------------------
        self.contacts: Dict[str, List[datetime]] = {}          # customer -> ts
        self.voice_calls: Dict[str, List[datetime]] = {}
        self.money_attempts: Dict[str, int] = {}               # order -> n
        self.actions_taken: Dict[str, int] = {}                # order -> n
        self.opted_out: set = set()
        self.promises: Dict[str, datetime] = {}                # order -> promised date
        self.spend_inr: float = 0.0
        self.incentive_inr: float = 0.0
        self.halted: bool = False
        # Optional BudgetGovernor. When present it rations a binding budget by
        # expected-value density instead of letting arrival order decide.
        self.governor = governor

    # -- helpers ------------------------------------------------------------
    def _quiet_hours(self) -> Tuple[time, time]:
        q = self.cfg["contact"]["quiet_hours_ist"]
        sh, sm = [int(x) for x in q["start"].split(":")]
        eh, em = [int(x) for x in q["end"].split(":")]
        return time(sh, sm), time(eh, em)

    def _in_quiet_hours(self, ts: datetime) -> bool:
        start, end = self._quiet_hours()
        t = ts.time()
        # Window wraps midnight (21:00 -> 09:00).
        return t >= start or t < end

    def prior(self, cls: FailureClass, iv: Intervention) -> float:
        return self.priors.get(cls.value, {}).get(iv.value, 0.05)

    def next_retry_time(self, now: datetime, attempt_idx: int, payday_aware: bool) -> datetime:
        backoff = self.cfg["retries"]["backoff_minutes"]
        mins = backoff[min(attempt_idx, len(backoff) - 1)]
        jitter = self.cfg["retries"]["jitter_pct"] / 100.0
        # Deterministic jitter: seeded per-call by the caller in practice; here
        # a fixed spread keeps replays identical.
        mins = mins * (1.0 + jitter * 0.5)
        t = now + timedelta(minutes=mins)
        if payday_aware:
            # Balance-driven failures recover around salary credit. Landing the
            # retry on the 1st-3rd or just after the 28th beats brute force.
            if 4 <= t.day <= 27:
                nxt = t.replace(day=28, hour=11, minute=0, second=0, microsecond=0)
                if nxt > t:
                    t = nxt
        return t

    # -- the candidate menu -------------------------------------------------
    def candidates(self, cls: FailureClass) -> List[Intervention]:
        return list(PLAYBOOK.get(cls, []))

    # -- the gate -----------------------------------------------------------
    def evaluate(
        self,
        proposal: Proposal,
        order: Order,
        customer: CustomerView,
        cls: FailureClass,
        now: datetime,
        dry_run: bool = False,
    ) -> Verdict:
        """Run every rule. Short-circuits on the first denial, but records
        everything evaluated up to that point.

        `dry_run=True` is used to probe candidate actions before choosing one.
        It must not mutate engine state -- an early version tripped the batch
        budget halt while merely *considering* an expensive voice call, which
        killed the rest of the run. Probes are free; only real executions move
        state.
        """
        rules: List[RuleResult] = []
        iv = proposal.intervention
        ch = ACTION_CHANNEL[iv]
        cc = self.cfg["contact"]
        v = Verdict(allowed=False, proposal=proposal, rules=rules)

        def deny(rule: str, detail: str, stop: bool = False, reason: Optional[str] = None) -> Verdict:
            rules.append(RuleResult(rule, False, detail))
            v.denial_rule, v.denial_detail = rule, detail
            v.allowed = False
            if stop:
                v.stop_sequence, v.stop_reason = True, reason or rule
            return v

        def ok(rule: str, detail: str = "") -> None:
            rules.append(RuleResult(rule, True, detail))

        # 1. batch budget halt -------------------------------------------
        if self.halted:
            return deny("budget.batch_halted", "batch spend cap already breached", stop=True, reason="BUDGET_HALT")
        ok("budget.batch_halted", "batch still within cap")

        # 2. permanent suppression ---------------------------------------
        if customer.opted_out or order.customer_id in self.opted_out:
            return deny(
                "contact.opt_out_is_permanent",
                "customer has opted out; suppression is permanent across channels and batches",
                stop=True, reason="OPT_OUT",
            )
        ok("contact.opt_out_is_permanent", "no opt-out on record")

        # 3. hard escalation flags ---------------------------------------
        esc = set(self.cfg["approval"]["always_escalate_flags"])
        hit = [f.value for f in customer.flags if f.value in esc]
        if hit and iv != I.HUMAN_COLLECTIONS_CALL:
            v.requires_human = True
            return deny(
                "approval.always_escalate_flags",
                f"customer carries {hit}; autonomous action not permitted",
                stop=True, reason="HUMAN_ESCALATED",
            )
        ok("approval.always_escalate_flags", "no blocking flags")

        # 4. promise-to-pay freeze ---------------------------------------
        if self.cfg["stopping"]["promise_to_pay_pauses_sequence"]:
            p = self.promises.get(order.order_id)
            if p is not None:
                grace = timedelta(days=self.cfg["stopping"]["promise_to_pay_grace_days"])
                if now <= p + grace:
                    return deny(
                        "stopping.promise_to_pay_pauses_sequence",
                        f"customer promised to pay by {p:%Y-%m-%d}; sequence frozen until "
                        f"{(p + grace):%Y-%m-%d}",
                    )
            ok("stopping.promise_to_pay_pauses_sequence", "no active promise")

        # 5. per-order action ceiling ------------------------------------
        n_actions = self.actions_taken.get(order.order_id, 0)
        if n_actions >= self.cfg["stopping"]["max_actions_per_order"]:
            return deny(
                "stopping.max_actions_per_order",
                f"{n_actions} actions already taken (cap {self.cfg['stopping']['max_actions_per_order']})",
                stop=True, reason="MAX_ACTIONS",
            )
        ok("stopping.max_actions_per_order", f"{n_actions} actions so far")

        # 6. sequence age ------------------------------------------------
        age_days = (now - order.created_at).days
        if age_days > self.cfg["stopping"]["max_sequence_age_days"]:
            return deny(
                "stopping.max_sequence_age_days",
                f"order is {age_days}d old (cap {self.cfg['stopping']['max_sequence_age_days']})",
                stop=True, reason="TOO_OLD",
            )
        ok("stopping.max_sequence_age_days", f"order age {age_days}d")

        # 7. money-action rules ------------------------------------------
        if iv in MONEY_ACTIONS:
            never = set(self.cfg["retries"]["never_retry_classes"])
            if cls.value in never:
                return deny(
                    "retries.never_retry_classes",
                    f"{cls.value} is never retry-charged: a retry is futile or harmful",
                )
            ok("retries.never_retry_classes", f"{cls.value} is retryable")

            n = self.money_attempts.get(order.order_id, 0) + order.attempt_count
            cap = self.cfg["retries"]["max_attempts_per_order"]
            if n >= cap:
                return deny(
                    "retries.max_attempts_per_order",
                    f"{n} charge attempts already made (cap {cap})",
                    stop=True, reason="MAX_RETRIES",
                )
            ok("retries.max_attempts_per_order", f"{n}/{cap} charge attempts used")

        # 8. contact-channel rules ---------------------------------------
        if ch in (Channel.SMS, Channel.WHATSAPP, Channel.VOICE, Channel.EMAIL):
            # consent
            need_consent = set(cc["consent_required_channels"])
            if ch.value in need_consent:
                has = customer.consent_whatsapp if ch == Channel.WHATSAPP else customer.consent_voice
                if not has:
                    return deny(
                        "contact.consent_required_channels",
                        f"no stored consent for {ch.value}",
                    )
                ok("contact.consent_required_channels", f"consent on file for {ch.value}")

            # DND registry
            dnd_channels = set(self.cfg["contact"]["honour_dnd"])
            if customer.dnd_registered and iv.value in dnd_channels:
                return deny(
                    "contact.honour_dnd",
                    f"customer is DND-registered; {iv.value} not permitted regardless of consent",
                )
            ok("contact.honour_dnd", "DND check passed")

            # quiet hours -- email is asynchronous and exempt
            if ch != Channel.EMAIL and self._in_quiet_hours(now):
                q = self.cfg["contact"]["quiet_hours_ist"]
                return deny(
                    "contact.quiet_hours_ist",
                    f"{now:%H:%M} IST falls in quiet hours {q['start']}-{q['end']}",
                )
            ok("contact.quiet_hours_ist", f"{now:%H:%M} IST outside quiet hours")

            # frequency caps
            hist = self.contacts.get(order.customer_id, [])
            in24 = [t for t in hist if (now - t) < timedelta(hours=24)]
            in7d = [t for t in hist if (now - t) < timedelta(days=7)]
            if len(in24) >= cc["max_contacts_per_customer_per_24h"]:
                return deny(
                    "contact.max_contacts_per_customer_per_24h",
                    f"{len(in24)} contacts in last 24h (cap {cc['max_contacts_per_customer_per_24h']})",
                )
            ok("contact.max_contacts_per_customer_per_24h", f"{len(in24)} in 24h")
            if len(in7d) >= cc["max_contacts_per_customer_per_7d"]:
                return deny(
                    "contact.max_contacts_per_customer_per_7d",
                    f"{len(in7d)} contacts in last 7d (cap {cc['max_contacts_per_customer_per_7d']})",
                )
            ok("contact.max_contacts_per_customer_per_7d", f"{len(in7d)} in 7d")
            if hist:
                gap = (now - max(hist)).total_seconds() / 3600.0
                if gap < cc["min_hours_between_contacts"]:
                    return deny(
                        "contact.min_hours_between_contacts",
                        f"last contact {gap:.1f}h ago (min {cc['min_hours_between_contacts']}h)",
                    )
                ok("contact.min_hours_between_contacts", f"{gap:.1f}h since last contact")

        # 9. voice tier --------------------------------------------------
        if iv == I.VOICE_CALL:
            vc = self.cfg["voice"]
            if order.amount_inr < vc["min_amount_inr"]:
                return deny(
                    "voice.min_amount_inr",
                    f"Rs{order.amount_inr:,.0f} below voice threshold Rs{vc['min_amount_inr']:,.0f}",
                )
            ok("voice.min_amount_inr", f"Rs{order.amount_inr:,.0f} clears voice threshold")

            prior_contacts = len(self.contacts.get(order.customer_id, []))
            if prior_contacts < vc["requires_prior_failed_contacts"]:
                return deny(
                    "voice.requires_prior_failed_contacts",
                    f"only {prior_contacts} prior contacts; voice is never the first touch "
                    f"(needs {vc['requires_prior_failed_contacts']})",
                )
            ok("voice.requires_prior_failed_contacts", f"{prior_contacts} cheaper touches already tried")

            calls7 = [t for t in self.voice_calls.get(order.customer_id, []) if (now - t) < timedelta(days=7)]
            if len(calls7) >= vc["max_calls_per_customer_per_7d"]:
                return deny("voice.max_calls_per_customer_per_7d", f"{len(calls7)} calls in last 7d")
            ok("voice.max_calls_per_customer_per_7d", f"{len(calls7)} calls in 7d")

        # 10. human approval threshold -----------------------------------
        thr = self.cfg["approval"]["human_approval_above_inr"]
        if order.amount_inr > thr and iv != I.HUMAN_COLLECTIONS_CALL:
            v.requires_human = True
            return deny(
                "approval.human_approval_above_inr",
                f"Rs{order.amount_inr:,.0f} exceeds autonomous limit Rs{thr:,.0f}; "
                f"action prepared and parked for human approval",
                stop=True, reason="HUMAN_ESCALATED",
            )
        ok("approval.human_approval_above_inr", f"Rs{order.amount_inr:,.0f} within autonomous limit")

        # 11. cost proportionality ---------------------------------------
        b = self.cfg["budget"]
        cost = proposal.cost_inr
        if order.amount_inr > 0:
            pct = cost / order.amount_inr * 100
            if pct > b["max_action_cost_as_pct_of_amount"]:
                return deny(
                    "budget.max_action_cost_as_pct_of_amount",
                    f"action costs Rs{cost:.2f} = {pct:.1f}% of Rs{order.amount_inr:,.0f} "
                    f"(cap {b['max_action_cost_as_pct_of_amount']}%)",
                )
            ok("budget.max_action_cost_as_pct_of_amount", f"cost is {pct:.2f}% of order value")

        if self.spend_inr + cost > b["max_batch_spend_inr"]:
            if not dry_run:
                self.halted = True
            return deny(
                "budget.max_batch_spend_inr",
                f"Rs{self.spend_inr:.2f} + Rs{cost:.2f} would breach batch cap "
                f"Rs{b['max_batch_spend_inr']:,.0f}",
                stop=True, reason="BUDGET_HALT",
            )
        ok("budget.max_batch_spend_inr", f"Rs{self.spend_inr:.2f} spent of Rs{b['max_batch_spend_inr']:,.0f}")

        # --- budget rationing -------------------------------------------
        # Evaluated as a rule so the reason lands in the audit trace like any
        # other. A refusal here is not "we ran out of money"; it is "this rupee
        # buys more somewhere else in this batch".
        if self.governor is not None and cost > 0:
            ev_for_budget = proposal.believed_success_prob * order.amount_inr
            admitted, why = self.governor.admits(ev_for_budget, cost, order.amount_inr)
            if not admitted:
                return deny("budget.below_shadow_price", why)
            ok("budget.below_shadow_price", why)

        # 12. incentive caps ---------------------------------------------
        if proposal.incentive_inr > 0:
            max_inc = order.amount_inr * b["max_incentive_pct_of_order"] / 100.0
            if proposal.incentive_inr > max_inc:
                return deny(
                    "budget.max_incentive_pct_of_order",
                    f"incentive Rs{proposal.incentive_inr:.2f} exceeds {b['max_incentive_pct_of_order']}% "
                    f"of order (Rs{max_inc:.2f})",
                )
            if self.incentive_inr + proposal.incentive_inr > b["max_batch_incentive_inr"]:
                return deny("budget.max_batch_incentive_inr", "batch incentive budget exhausted")
            ok("budget.max_incentive_pct_of_order", f"incentive Rs{proposal.incentive_inr:.2f} within cap")

        # 13. economic stopping rule -------------------------------------
        # This is what stops the agent harassing low-value orders: if the
        # expected recovery does not clear a multiple of the action's cost,
        # doing nothing is the correct choice.
        ev = proposal.believed_success_prob * order.amount_inr
        min_ratio = self.cfg["stopping"]["min_expected_value_ratio"]
        if cost > 0 and ev < min_ratio * cost:
            return deny(
                "stopping.min_expected_value_ratio",
                f"EV Rs{ev:.2f} < {min_ratio}x cost Rs{cost:.2f}; not economic to pursue",
                stop=True, reason="UNECONOMIC",
            )
        ok("stopping.min_expected_value_ratio", f"EV Rs{ev:.2f} vs cost Rs{cost:.2f}")

        v.allowed = True
        return v

    # -- state transitions (only called after a successful execution) -------
    def record_execution(self, proposal: Proposal, order: Order, now: datetime) -> None:
        iv = proposal.intervention
        self.actions_taken[order.order_id] = self.actions_taken.get(order.order_id, 0) + 1
        self.spend_inr += proposal.cost_inr
        self.incentive_inr += proposal.incentive_inr
        if iv in MONEY_ACTIONS:
            self.money_attempts[order.order_id] = self.money_attempts.get(order.order_id, 0) + 1
        ch = ACTION_CHANNEL[iv]
        if ch in (Channel.SMS, Channel.WHATSAPP, Channel.VOICE, Channel.EMAIL):
            self.contacts.setdefault(order.customer_id, []).append(now)
        if iv == I.VOICE_CALL:
            self.voice_calls.setdefault(order.customer_id, []).append(now)

    def earliest_retry_time(self, customer_id: str, now: datetime) -> datetime:
        """When will the transient contact constraints on this customer clear?

        Requeueing at a flat "+8 hours" would either waste cycles re-probing a
        cap that has not moved, or overshoot and delay a recoverable order by a
        day. Computing the actual clearing time keeps the sequence tight.
        """
        cc = self.cfg["contact"]
        hist = sorted(self.contacts.get(customer_id, []))
        candidates = [now + timedelta(hours=1)]

        if hist:
            last = hist[-1]
            candidates.append(last + timedelta(hours=cc["min_hours_between_contacts"]))
            in24 = [t for t in hist if (now - t) < timedelta(hours=24)]
            if len(in24) >= cc["max_contacts_per_customer_per_24h"]:
                candidates.append(min(in24) + timedelta(hours=24, minutes=5))
            in7d = [t for t in hist if (now - t) < timedelta(days=7)]
            if len(in7d) >= cc["max_contacts_per_customer_per_7d"]:
                candidates.append(min(in7d) + timedelta(days=7, minutes=5))

        t = max(candidates)
        # Never wake up inside quiet hours -- step forward to when comms reopen.
        if self._in_quiet_hours(t):
            _, end = self._quiet_hours()
            nxt = t.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
            if nxt <= t:
                nxt = nxt + timedelta(days=1)
            t = nxt
        return t

    def record_opt_out(self, customer_id: str) -> None:
        self.opted_out.add(customer_id)

    def record_promise(self, order_id: str, promised_for: datetime) -> None:
        self.promises[order_id] = promised_for
