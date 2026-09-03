"""The recovery loop: detect -> diagnose -> propose -> gate -> execute -> observe.

Implemented as a global event queue ordered by simulated time, not as a
per-order for-loop. That choice matters: contact frequency caps and quiet hours
are defined *per customer across orders*, so processing one order's whole
sequence before starting the next would let a customer with three failed orders
be messaged three times in a minute while every individual sequence looked
compliant. A single time-ordered queue makes the caps actually bind.

Three arms share this loop, differing only in how they diagnose and choose:

    A_BASELINE   fixed retry schedule, no diagnosis, no comms.
                 What a merchant gets out of the box.
    B_RULES      deterministic diagnosis + playbook + full policy gate.
                 No model anywhere.
    C_AGENT      tiered diagnosis (LLM on the ambiguous slice) + playbook
                 ranked by expected value + full policy gate.

Keeping the executor, the gate and the world identical across arms is what
makes the comparison mean anything: the only thing that varies is the decision
policy.
"""
from __future__ import annotations

import heapq
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .detect import DegradationSignal, RiskFacts, build_risk_register, detect_degradations
from .diagnose import Diagnoser, Diagnosis
from .executor import Executor
from .ledger import Actor, EventType, Ledger
from .models import (
    ACTION_CHANNEL,
    ACTION_COST_INR,
    Batch,
    Channel,
    FailureClass,
    Intervention,
    Order,
)
from .channels.voice import VoiceProvider, build_script
from .budget import BudgetGovernor, Projection
from .policy import PolicyEngine, Proposal, classify_denial
from .simulate import World

I = Intervention

# A comms action may be repeated at most twice; real dunning ladders do use a
# second touch on the same channel days later, but a third is harassment and the
# measured response decay makes it worthless anyway.
MAX_REPEATS_PER_COMMS_ACTION = 2

ARM_A = "A_BASELINE"
ARM_B = "B_RULES"
ARM_C = "C_AGENT"


@dataclass
class OrderState:
    order: Order
    diagnosis: Optional[Diagnosis] = None
    step: int = 0
    money_attempt_idx: int = 0
    contact_idx: int = 0
    recovered: bool = False
    recovered_via: Optional[str] = None
    stopped: bool = False
    stop_reason: Optional[str] = None
    actions: List[str] = field(default_factory=list)
    spend_inr: float = 0.0
    contacts_made: int = 0
    escalated: bool = False
    blocked_by: List[str] = field(default_factory=list)


@dataclass
class RunResult:
    arm: str
    states: Dict[str, OrderState]
    signals: List[DegradationSignal]
    coverage: Dict
    ledger: Ledger
    diagnoses: Dict[str, Diagnosis]
    policy: PolicyEngine
    executor: Executor
    diagnoser_stats: Dict
    holdout_ids: List[str]
    treated_ids: List[str]
    human_queue: List[Dict] = field(default_factory=list)
    wall_seconds: float = 0.0
    razorpay_stats: Dict = field(default_factory=dict)
    voice_stats: Dict = field(default_factory=dict)
    governor_stats: Dict = field(default_factory=dict)
    diagnosis_timing: Dict = field(default_factory=dict)


class Orchestrator:
    def __init__(
        self,
        batch: Batch,
        arm: str,
        policy_path: str,
        priors_path: Optional[str],
        ledger_path: str,
        diagnoser: Optional[Diagnoser] = None,
        chaos: float = 0.0,
        seed: int = 20260903,
        razorpay=None,
        voice: Optional[VoiceProvider] = None,
        merchant_name: str = "Kirana Kart",
        diagnose_workers: int = 12,
        budget_governor: bool = True,
    ):
        self.diagnose_workers = diagnose_workers
        self.diagnosis_timing: Dict = {}
        self.batch = batch
        self.voice = voice
        self.merchant_name = merchant_name
        self.arm = arm
        self.seed = seed
        self.world = World(batch, seed=seed)
        gov = None
        if budget_governor:
            import yaml as _yaml
            with open(policy_path) as _fh:
                _cfg = _yaml.safe_load(_fh)
            gov = BudgetGovernor(budget_inr=float(_cfg["budget"]["max_batch_spend_inr"]))
        self.governor = gov
        self.policy = PolicyEngine(policy_path, priors_path, governor=gov)
        self.executor = Executor(self.world, chaos=chaos, seed=seed + 1, razorpay=razorpay)
        self.diagnoser = diagnoser
        self.ledger = Ledger(ledger_path, run_id=f"{batch.batch_id}:{arm}", policy_version=self.policy.version)
        self.states: Dict[str, OrderState] = {}
        self.diagnoses: Dict[str, Diagnosis] = {}
        self.signals: List[DegradationSignal] = []
        self.coverage: Dict = {}
        self.register: Dict[str, RiskFacts] = {}
        # Orders a person must look at. Part of the deliverable, not a leftover.
        self.human_queue: List[Dict] = []

    # -- proposal generation -------------------------------------------------
    def _propose(
        self, st: OrderState, cls: FailureClass, now: datetime
    ) -> Tuple[Optional[Proposal], List[Dict]]:
        """Pick the next action, and return why every candidate won or lost.

        Arm A ignores the diagnosis entirely and just retries on a schedule.
        Arms B and C draw candidates from the playbook for the diagnosed class,
        probe each against the policy gate, and take the one with the best
        expected value net of cost.

        The probe trace is returned rather than discarded. An earlier version
        threw it away, and the result was a ledger in which 195 orders simply
        said NO_ELIGIBLE_ACTION with no explanation -- the audit trail could
        answer "why did we act?" but not "why didn't we?", which is exactly the
        question a merchant asks about money that never came back.
        """
        order = st.order
        trace: List[Dict] = []

        if self.arm == ARM_A:
            # The out-of-the-box behaviour: retry, wait, retry, wait, retry.
            if st.money_attempt_idx >= 3:
                return None, [{"intervention": "RETRY_SCHEDULED", "allowed": False,
                               "denial_rule": "baseline.fixed_schedule_exhausted"}]
            return Proposal(
                order_id=order.order_id,
                intervention=I.RETRY_SCHEDULED,
                rationale="fixed retry schedule (baseline; no diagnosis)",
                believed_success_prob=0.35,       # a flat, uninformed guess
                scheduled_for=now,
            ), []

        # How many times may each action be repeated?
        #
        # The first version refused to repeat ANY action, which quietly wasted
        # the retry budget: `policy.yaml` permits 3 charge attempts with
        # exponential backoff and payday-aware scheduling, but the orchestrator
        # would only ever fire one. A second retry timed to land after salary
        # credit is a materially different action from the first, not a repeat
        # -- 239 candidate rejections worth Rs4.1L were this bug.
        #
        # Comms are different: the same SMS twice in an hour is spam. So repeats
        # are allowed but capped per action, and the contact frequency rules in
        # the gate plus the simulator's response decay govern whether a second
        # touch is worth anything.
        money_cap = self.policy.cfg["retries"]["max_attempts_per_order"]
        used = Counter(st.actions)

        candidates = self.policy.candidates(cls)
        if not candidates:
            return None, [{"intervention": None, "allowed": False,
                           "denial_rule": "playbook.empty",
                           "denial_detail": f"no intervention is permitted for {cls.value}"}]

        scored: List[Tuple[float, Proposal]] = []
        for iv in candidates:
            is_money = iv in (I.RETRY_NOW, I.RETRY_SCHEDULED, I.MANDATE_REPRESENT)
            repeat_cap = money_cap if is_money else MAX_REPEATS_PER_COMMS_ACTION
            if used[iv.value] >= repeat_cap:
                trace.append({
                    "intervention": iv.value, "allowed": False,
                    "denial_rule": "orchestrator.repeat_cap",
                    "denial_detail": f"already used {used[iv.value]}x (cap {repeat_cap} for "
                                     f"{'money' if is_money else 'comms'} actions)",
                })
                continue
            p = self.policy.prior(cls, iv)
            sched = now
            if used[iv.value] > 0 and not is_money:
                # A second touch on the same channel waits for the frequency
                # window to clear rather than stacking on the first.
                sched = max(now, self.policy.earliest_retry_time(order.customer_id, now))
            if iv in (I.RETRY_SCHEDULED, I.MANDATE_REPRESENT):
                payday = cls in (
                    FailureClass.INSUFFICIENT_FUNDS,
                    FailureClass.MANDATE_INSUFFICIENT,
                )
                sched = self.policy.next_retry_time(now, st.money_attempt_idx, payday)
            elif iv == I.RETRY_NOW:
                sched = now + timedelta(minutes=2)

            prop = Proposal(
                order_id=order.order_id,
                intervention=iv,
                rationale=f"playbook candidate for {cls.value}",
                believed_success_prob=p,
                scheduled_for=sched,
            )
            # Probe the gate; a candidate that would be denied is not a candidate.
            probe = self.policy.evaluate(
                prop, order, self.batch.customers[order.customer_id], cls, sched, dry_run=True
            )
            trace.append({
                "intervention": iv.value,
                "allowed": probe.allowed,
                "denial_rule": probe.denial_rule,
                "denial_detail": probe.denial_detail,
                "believed_success_prob": round(p, 4),
                "cost_inr": prop.cost_inr,
                "expected_value_inr": round(p * order.amount_inr, 2),
            })
            if not probe.allowed:
                continue
            # Net expected value. Arms B and C both use this; the difference
            # between them is the quality of `cls`, which drives which
            # candidates exist and what the priors say.
            # Rank by incremental recovery, not raw response.
            ev = self.policy.score(cls, iv) * order.amount_inr - prop.cost_inr
            scored.append((ev, prop))

        if not scored:
            return None, trace
        scored.sort(key=lambda t: -t[0])
        return scored[0][1], trace

    def _diagnose_all(self, order_ids: List[str]) -> None:
        """Diagnose every treated order concurrently, then log deterministically."""
        import time as _t
        from concurrent.futures import ThreadPoolExecutor

        results: Dict[str, Diagnosis] = {}
        latencies: Dict[str, float] = {}

        def one(oid: str):
            t0 = _t.perf_counter()
            d = self.diagnoser.diagnose(self.register[oid])
            return oid, d, (_t.perf_counter() - t0) * 1000.0

        t0 = _t.perf_counter()
        # Modest pool: enough to hide network latency, small enough not to trip
        # provider rate limits on a 450-call batch.
        with ThreadPoolExecutor(max_workers=self.diagnose_workers) as pool:
            for oid, d, ms in pool.map(one, order_ids):
                results[oid] = d
                latencies[oid] = ms
        wall = _t.perf_counter() - t0

        cust_of = {o.order_id: o.customer_id for o in self.batch.orders}
        # Sorted, so the ledger does not depend on thread completion order.
        for oid in sorted(results):
            d = results[oid]
            self.diagnoses[oid] = d
            self.ledger.append(
                EventType.DIAGNOSIS, Actor.DIAGNOSER,
                order_id=oid, customer_id=cust_of.get(oid),
                arm=self.arm, model_version=d.model_version,
                prompt_hash=d.prompt_hash, cost_inr=d.cost_inr,
                payload={
                    "failure_class": d.failure_class.value,
                    "confidence": d.confidence,
                    "tier": d.tier,
                    "evidence": d.evidence,
                    "reasoning": d.reasoning,
                    "fallback_reason": d.fallback_reason,
                    "latency_ms": round(latencies[oid], 1),
                },
            )

        vals = sorted(latencies.values())
        if vals:
            self.diagnosis_timing = {
                "orders": len(vals),
                "wall_seconds": round(wall, 2),
                "throughput_per_second": round(len(vals) / wall, 1) if wall > 0 else None,
                "workers": self.diagnose_workers,
                "p50_ms": round(vals[len(vals) // 2], 1),
                "p95_ms": round(vals[int(len(vals) * 0.95)], 1),
                "max_ms": round(vals[-1], 1),
            }

    # -- the run -------------------------------------------------------------
    def _plan_budget(self, treated_ids: List[str]) -> None:
        """Project each order's best paid action and solve for the shadow price.

        Runs after diagnosis because the projection needs a predicted failure
        class to know which playbook applies and what the priors say.
        """
        if self.governor is None:
            return
        projections: List[Projection] = []
        for oid in treated_ids:
            d = self.diagnoses.get(oid)
            if d is None:
                continue
            order = self.states[oid].order
            # Walk the whole ladder, not just the best rung. `reach` decays as
            # each cheaper rung is assumed to have been tried and failed, which
            # is what makes the expensive escalations cost what they really
            # cost in expectation.
            reach = 1.0
            for iv in self.policy.candidates(d.failure_class):
                p_iv = self.policy.prior(d.failure_class, iv)
                u_iv = self.policy.score(d.failure_class, iv)
                cost = ACTION_COST_INR[iv]
                if cost > 0:
                    # Comms rungs may be used more than once (MAX_REPEATS_PER_
                    # COMMS_ACTION), so a single projection per rung understated
                    # demand and left the shadow price too low to ration
                    # anything in the mid-budget range.
                    repeats = (
                        MAX_REPEATS_PER_COMMS_ACTION
                        if iv not in (I.RETRY_NOW, I.RETRY_SCHEDULED, I.MANDATE_REPRESENT)
                        else 1
                    )
                    r_k = reach
                    for _ in range(repeats):
                        projections.append(Projection(
                            order_id=oid, intervention=iv.value,
                            amount_inr=order.amount_inr, believed_p=u_iv,
                            cost_inr=cost, reach_prob=r_k,
                        ))
                        r_k *= max(0.0, 1.0 - p_iv)
                # Free or paid, a rung that succeeds ends the sequence.
                reach *= max(0.0, 1.0 - p_iv)

        plan = self.governor.plan(projections)
        self.ledger.append(
            EventType.BUDGET_PLANNED, Actor.POLICY, arm=self.arm, payload=plan,
        )

    def run(self, max_steps_per_order: int = 10) -> RunResult:
        import time as _time

        t0 = _time.perf_counter()
        b = self.batch
        now0 = b.generated_at

        self.ledger.append(
            EventType.BATCH_OPEN, Actor.SYSTEM,
            payload={
                "arm": self.arm,
                "orders": len(b.orders),
                "total_at_risk_inr": b.total_at_risk_inr,
                "policy_version": self.policy.version,
                "priors_source": self.policy.priors_source,
                "chaos": self.executor.chaos,
                "seed": self.seed,
            },
        )

        # --- detection (arms B and C; the baseline has no detector) ---------
        if self.arm != ARM_A:
            self.signals, self.coverage = detect_degradations(b.traffic, return_coverage=True)
            for s in self.signals:
                self.ledger.append(
                    EventType.SIGNAL_RAISED, Actor.DETECTOR,
                    payload=s.to_dict(), arm=self.arm,
                )
        self.register = build_risk_register(b, self.signals, now=now0)

        holdout, treated = [], []
        queue: List[Tuple[datetime, int, str]] = []
        tiebreak = 0
        for o in b.orders:
            self.states[o.order_id] = OrderState(order=o)
            if o.is_holdout:
                holdout.append(o.order_id)
            else:
                treated.append(o.order_id)
                tiebreak += 1
                heapq.heappush(queue, (now0, tiebreak, o.order_id))

        # --- diagnosis pass, concurrent -------------------------------------
        # Diagnosis is per-order independent and network-bound, so running it
        # inside the event loop meant ~450 sequential API calls and a 15-minute
        # batch. Running it as a concurrent pre-pass cuts that to well under a
        # minute, and throughput is part of what this system claims.
        #
        # Results are collected into a dict and logged in sorted order, so the
        # ledger is identical across runs regardless of which thread finished
        # first. Concurrency must not cost reproducibility.
        if self.arm != ARM_A and self.diagnoser is not None:
            # Skip orders whose organic recovery has already landed by batch
            # start. The lazy in-loop path got this for free; the pre-pass has
            # to filter explicitly, or parallelising would quietly start paying
            # the model to diagnose orders that were already resolved.
            to_diagnose = [
                oid for oid in treated
                if not (
                    (h := self.world.self_heal_at(
                        self.states[oid].order, self.states[oid].order.created_at)) is not None
                    and h <= now0
                )
            ]
            self._diagnose_all(to_diagnose)
            self._plan_budget(treated)

        # --- holdout: observe only, never touch ----------------------------
        # This arm exists to measure the counterfactual. Nothing is executed,
        # so anything that recovers here recovered on its own.
        horizon = now0 + timedelta(days=self.policy.cfg["stopping"]["max_sequence_age_days"])
        for oid in holdout:
            st = self.states[oid]
            heal_at = self.world.self_heal_at(st.order, st.order.created_at)
            if heal_at is not None and heal_at <= horizon:
                st.recovered = True
                st.recovered_via = "self_heal"
                st.order.recovered = True
                st.order.recovered_at = heal_at
                st.order.recovered_amount_inr = st.order.amount_inr
            self.ledger.append(
                EventType.OUTCOME_OBSERVED, Actor.SIMULATOR,
                order_id=oid, customer_id=st.order.customer_id, arm="HOLDOUT",
                payload={
                    "recovered": st.recovered,
                    "via": st.recovered_via,
                    "amount_inr": st.order.amount_inr,
                    "note": "holdout: no intervention taken, observation only",
                },
            )

        # --- treated: event-driven recovery loop ---------------------------
        while queue:
            now, _, oid = heapq.heappop(queue)
            st = self.states[oid]
            if st.recovered or st.stopped or st.step >= max_steps_per_order:
                continue
            order = st.order
            cust = b.customers[order.customer_id]

            # Self-heal competes with the agent on the timeline rather than
            # pre-empting it. If the organic recovery would already have landed
            # by now, it wins -- and any contacts the agent made before that
            # point were wasted, which is what makes the false-positive cost
            # measurable instead of structurally zero.
            heal_at = self.world.self_heal_at(order, order.created_at)
            if heal_at is not None and heal_at <= now:
                st.recovered = True
                st.recovered_via = "self_heal"
                order.recovered = True
                order.recovered_at = heal_at
                order.recovered_amount_inr = order.amount_inr
                self.ledger.append(
                    EventType.OUTCOME_OBSERVED, Actor.SIMULATOR,
                    order_id=oid, customer_id=order.customer_id, arm=self.arm,
                    payload={
                        "recovered": True, "via": "self_heal",
                        "amount_inr": order.amount_inr,
                        "healed_at": heal_at.isoformat(),
                        "contacts_already_spent": st.contacts_made,
                        "note": "recovered without intervention; NOT attributable to the "
                                "agent. Any contacts already sent were wasted.",
                    },
                )
                continue

            # --- diagnose once -------------------------------------------
            if st.diagnosis is None:
                if self.arm == ARM_A:
                    st.diagnosis = Diagnosis(
                        order_id=oid, failure_class=FailureClass.UNKNOWN,
                        confidence=0.0, tier="none",
                        reasoning="baseline arm performs no diagnosis",
                    )
                else:
                    # Filled by the concurrent pre-pass; only computed here if
                    # an order somehow reaches the loop without one.
                    st.diagnosis = self.diagnoses.get(oid) or self.diagnoser.diagnose(
                        self.register[oid]
                    )
                self.diagnoses[oid] = st.diagnosis

            cls = st.diagnosis.failure_class
            st.step += 1

            # --- propose --------------------------------------------------
            prop, probe_trace = self._propose(st, cls, now)
            if prop is None:
                denials = [c["denial_rule"] for c in probe_trace if not c["allowed"]]
                kinds = {classify_denial(r) for r in denials}

                # A person must decide. Park it -- never drop it. These orders
                # become the human-review section of the exception list.
                if "HUMAN" in kinds:
                    st.escalated = True
                    st.stopped = True
                    st.stop_reason = "HUMAN_ESCALATED"
                    rule = next(r for r in denials if classify_denial(r) == "HUMAN")
                    detail = next(c["denial_detail"] for c in probe_trace
                                  if c["denial_rule"] == rule)
                    self.human_queue.append({
                        "order_id": oid,
                        "customer_id": order.customer_id,
                        "amount_inr": order.amount_inr,
                        "diagnosed_class": cls.value,
                        "reason": rule,
                        "detail": detail,
                        "prepared_candidates": [c["intervention"] for c in probe_trace],
                    })
                    self.ledger.append(
                        EventType.HUMAN_ESCALATED, Actor.POLICY,
                        order_id=oid, customer_id=order.customer_id, arm=self.arm,
                        payload={"reason": rule, "detail": detail,
                                 "candidate_trace": probe_trace,
                                 "note": "parked for human approval; not actioned autonomously"},
                    )
                    continue

                # The constraint clears with time. Come back when it does
                # instead of abandoning recoverable money.
                if "TRANSIENT" in kinds and st.step < max_steps_per_order:
                    when = self.policy.earliest_retry_time(order.customer_id, now)
                    tiebreak += 1
                    heapq.heappush(queue, (when, tiebreak, oid))
                    self.ledger.append(
                        EventType.POLICY_VERDICT, Actor.POLICY,
                        order_id=oid, customer_id=order.customer_id, arm=self.arm,
                        payload={
                            "allowed": False,
                            "denial_rule": next(r for r in denials
                                                if classify_denial(r) == "TRANSIENT"),
                            "denial_detail": "transient constraint; sequence deferred",
                            "deferred_until": when.isoformat(),
                            "rules_evaluated": probe_trace,
                        },
                    )
                    continue

                st.stopped = True
                st.stop_reason = "NO_ELIGIBLE_ACTION"
                # Record every candidate and the rule that rejected it. This is
                # the difference between "the agent did nothing" and "the agent
                # considered five actions and the gate refused all five, here".
                blocking = [c["denial_rule"] for c in probe_trace if not c["allowed"]]
                st.blocked_by = blocking
                self.ledger.append(
                    EventType.SEQUENCE_STOPPED, Actor.POLICY,
                    order_id=oid, customer_id=order.customer_id, arm=self.arm,
                    payload={
                        "reason": "NO_ELIGIBLE_ACTION",
                        "detail": "every candidate action was refused by the gate",
                        "candidates_considered": len(probe_trace),
                        "candidate_trace": probe_trace,
                    },
                )
                continue

            at = prop.scheduled_for or now
            self.ledger.append(
                EventType.ACTION_PROPOSED, Actor.DIAGNOSER if self.arm != ARM_A else Actor.SYSTEM,
                order_id=oid, customer_id=order.customer_id, arm=self.arm,
                payload={
                    "intervention": prop.intervention.value,
                    "rationale": prop.rationale,
                    "believed_success_prob": round(prop.believed_success_prob, 4),
                    "cost_inr": prop.cost_inr,
                    "scheduled_for": at.isoformat(),
                    "expected_value_inr": round(prop.believed_success_prob * order.amount_inr, 2),
                    # The alternatives that lost, and why. Makes the choice
                    # reviewable rather than asserted.
                    "candidate_trace": probe_trace,
                },
            )

            # --- gate -----------------------------------------------------
            verdict = self.policy.evaluate(prop, order, cust, cls, at)
            self.ledger.append(
                EventType.POLICY_VERDICT, Actor.POLICY,
                order_id=oid, customer_id=order.customer_id, arm=self.arm,
                payload=verdict.to_dict(),
            )

            if not verdict.allowed:
                if verdict.requires_human:
                    st.escalated = True
                    self.ledger.append(
                        EventType.HUMAN_ESCALATED, Actor.POLICY,
                        order_id=oid, customer_id=order.customer_id, arm=self.arm,
                        payload={"reason": verdict.denial_rule, "detail": verdict.denial_detail,
                                 "prepared_action": prop.intervention.value},
                    )
                if verdict.stop_sequence:
                    st.stopped = True
                    st.stop_reason = verdict.stop_reason
                    self.ledger.append(
                        EventType.SEQUENCE_STOPPED, Actor.POLICY,
                        order_id=oid, customer_id=order.customer_id, arm=self.arm,
                        payload={"reason": verdict.stop_reason, "detail": verdict.denial_detail},
                    )
                    continue
                # Denied but not terminal: try again later in simulated time.
                tiebreak += 1
                heapq.heappush(queue, (now + timedelta(hours=6), tiebreak, oid))
                continue

            # --- execute --------------------------------------------------
            idx = st.money_attempt_idx if prop.intervention in (
                I.RETRY_NOW, I.RETRY_SCHEDULED, I.MANDATE_REPRESENT
            ) else st.contact_idx
            res = self.executor.execute(order, prop.intervention, idx, at)

            if not res.ok:
                self.ledger.append(
                    EventType.ACTION_FAILED, Actor.EXECUTOR,
                    order_id=oid, customer_id=order.customer_id, arm=self.arm,
                    payload={
                        "intervention": prop.intervention.value,
                        "error": res.error,
                        "attempts_made": res.attempts_made,
                        "reconciled": res.reconciled,
                        "dead_lettered": res.dead_lettered,
                        "idem_key": res.idem_key,
                        "note": "execution failed; order stays live for the next sweep "
                                "rather than being silently dropped",
                    },
                )
                # Failure to execute is not failure to recover -- requeue.
                tiebreak += 1
                heapq.heappush(queue, (now + timedelta(hours=12), tiebreak, oid))
                continue

            # Commit state only on a real execution.
            self.policy.record_execution(prop, order, at)
            st.actions.append(prop.intervention.value)
            st.spend_inr += prop.cost_inr
            if prop.intervention in (I.RETRY_NOW, I.RETRY_SCHEDULED, I.MANDATE_REPRESENT):
                st.money_attempt_idx += 1
            if ACTION_CHANNEL[prop.intervention] in (
                Channel.SMS, Channel.WHATSAPP, Channel.VOICE, Channel.EMAIL
            ):
                st.contact_idx += 1
                st.contacts_made += 1

            # Voice is the one channel whose CONTENT is itself a compliance
            # artifact, so the script and its disclosure flags go in the ledger
            # alongside the action. "We called them" is not auditable; "we called
            # them and here is every word, with AI disclosure at utterance one"
            # is.
            if prop.intervention == I.VOICE_CALL and self.voice is not None:
                script = build_script(
                    order_id=oid,
                    customer_id=order.customer_id,
                    amount_inr=order.amount_inr,
                    failure_class=cls.value,
                    locale=cust.locale,
                    merchant_name=self.merchant_name,
                    max_duration_seconds=self.policy.cfg["voice"]["max_call_duration_seconds"],
                )
                rec = self.voice.place_call(script)
                comp = script.to_dict()["compliance"]
                # Belt and braces: policy demands disclosure, the builder always
                # emits it, and we still refuse to record a call without it.
                assert comp["ai_disclosure_present"], "voice script missing AI disclosure"
                self.ledger.append(
                    EventType.ACTION_EXECUTED, Actor.EXECUTOR,
                    order_id=oid, customer_id=order.customer_id, arm=self.arm,
                    payload={
                        "intervention": "VOICE_CALL_SCRIPT",
                        "locale": cust.locale,
                        "amount_spoken": script.to_dict()["amount_spoken"],
                        "compliance": comp,
                        "line_tags": [l["tag"] for l in script.lines],
                        "transport_mode": rec["mode"],
                    },
                )

            self.ledger.append(
                EventType.ACTION_EXECUTED, Actor.EXECUTOR,
                order_id=oid, customer_id=order.customer_id, arm=self.arm,
                cost_inr=prop.cost_inr,
                payload={
                    "intervention": prop.intervention.value,
                    "idem_key": res.idem_key,
                    "replayed": res.replayed,
                    "attempts_made": res.attempts_made,
                    "reconciled": res.reconciled,
                    "razorpay_ref": res.razorpay_ref,
                    "executed_at": at.isoformat(),
                },
            )

            out = res.outcome
            if out is not None and out.recovered:
                st.recovered = True
                st.recovered_via = prop.intervention.value
                order.recovered = True
                order.recovered_at = at
                order.recovered_amount_inr = order.amount_inr
                self.ledger.append(
                    EventType.OUTCOME_OBSERVED, Actor.SIMULATOR,
                    order_id=oid, customer_id=order.customer_id, arm=self.arm,
                    payload={
                        "recovered": True, "via": prop.intervention.value,
                        "amount_inr": order.amount_inr, "detail": out.detail,
                    },
                )
                self.ledger.append(
                    EventType.SEQUENCE_STOPPED, Actor.POLICY,
                    order_id=oid, customer_id=order.customer_id, arm=self.arm,
                    payload={"reason": "SUCCESS", "detail": "stopping.stop_on_success"},
                )
                continue

            # --- side effects ---------------------------------------------
            if out is not None and out.side_effect == "OPT_OUT":
                self.policy.record_opt_out(order.customer_id)
                st.stopped = True
                st.stop_reason = "OPT_OUT"
                self.ledger.append(
                    EventType.OPT_OUT, Actor.SIMULATOR,
                    order_id=oid, customer_id=order.customer_id, arm=self.arm,
                    payload={"note": "customer opted out; permanent suppression applied "
                                     "across all channels and future batches"},
                )
                continue

            if out is not None and out.side_effect == "PROMISE_TO_PAY" and out.promised_for:
                self.policy.record_promise(oid, out.promised_for)
                self.ledger.append(
                    EventType.PROMISE_TO_PAY, Actor.SIMULATOR,
                    order_id=oid, customer_id=order.customer_id, arm=self.arm,
                    payload={"promised_for": out.promised_for.isoformat(),
                             "note": "sequence frozen until promise date + grace"},
                )
                # Resume just after the promise window.
                grace = self.policy.cfg["stopping"]["promise_to_pay_grace_days"]
                if self.world.honours_promise(order):
                    st.recovered = True
                    st.recovered_via = "PROMISE_KEPT"
                    order.recovered = True
                    order.recovered_at = out.promised_for
                    order.recovered_amount_inr = order.amount_inr
                    self.ledger.append(
                        EventType.OUTCOME_OBSERVED, Actor.SIMULATOR,
                        order_id=oid, customer_id=order.customer_id, arm=self.arm,
                        payload={"recovered": True, "via": "PROMISE_KEPT",
                                 "amount_inr": order.amount_inr},
                    )
                    continue
                tiebreak += 1
                heapq.heappush(queue, (out.promised_for + timedelta(days=grace + 1), tiebreak, oid))
                continue

            # No recovery, no side effect: try the next rung of the ladder.
            tiebreak += 1
            heapq.heappush(queue, (now + timedelta(hours=20), tiebreak, oid))

        self.ledger.append(
            EventType.BATCH_CLOSE, Actor.SYSTEM,
            payload={
                "arm": self.arm,
                "recovered": sum(1 for s in self.states.values() if s.recovered),
                "spend_inr": round(self.policy.spend_inr, 2),
                "ledger_entries": len(self.ledger.entries),
                "executor_stats": self.executor.stats,
                "budget_governor": self.governor.stats() if self.governor else None,
            },
        )

        return RunResult(
            arm=self.arm,
            states=self.states,
            signals=self.signals,
            coverage=self.coverage,
            ledger=self.ledger,
            diagnoses=self.diagnoses,
            policy=self.policy,
            executor=self.executor,
            diagnoser_stats=dict(self.diagnoser.stats) if self.diagnoser else {},
            holdout_ids=holdout,
            treated_ids=treated,
            human_queue=self.human_queue,
            governor_stats=self.governor.stats() if self.governor else {},
            diagnosis_timing=self.diagnosis_timing,
            wall_seconds=_time.perf_counter() - t0,
        )
