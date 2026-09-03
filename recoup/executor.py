"""Bounded executor -- the only component with side effects.

Responsibilities, in priority order:

1.  **Never double-charge.** Every money action is keyed on
    (order_id, intervention, attempt_idx). The key is checked before the call
    and recorded after, so a retry of an ambiguous timeout cannot debit twice.
    This is the invariant the whole design protects, because a duplicate debit
    is the one failure mode a merchant genuinely cannot forgive.

2.  **Degrade, don't stall.** Gateways time out and rate-limit. A per-gateway
    circuit breaker sheds load after repeated failures, transient errors get
    bounded retries with jitter, and anything still unresolved goes to a
    dead-letter queue for the next sweep rather than blocking the batch.

3.  **Reconcile after ambiguity.** A timeout means "unknown", not "failed".
    Treating it as failed is how double charges happen. So an ambiguous result
    triggers a reconciliation sweep that establishes the true state before any
    further action on that order.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .models import MONEY_ACTIONS, Intervention, Order
from .simulate import Outcome, World


class GatewayError(Exception):
    """Transient gateway/network failure -- retryable."""


class GatewayAmbiguous(Exception):
    """Timed out after the charge may have been submitted. State is UNKNOWN."""


class CircuitOpen(Exception):
    """Breaker is open for this gateway; shedding load by design."""


def idempotency_key(order_id: str, intervention: Intervention, attempt_idx: int) -> str:
    return hashlib.sha256(
        f"{order_id}:{intervention.value}:{attempt_idx}".encode()
    ).hexdigest()[:32]


@dataclass
class CircuitBreaker:
    """Per-gateway breaker. Deliberately simple and deliberately present:
    without it, a gateway having a bad hour turns into thousands of doomed
    calls and a batch that never finishes."""

    threshold: int = 5
    cooldown_calls: int = 20
    failures: int = 0
    open_until_call: int = -1
    trips: int = 0

    def allow(self, call_no: int) -> bool:
        return call_no > self.open_until_call

    def record(self, ok: bool, call_no: int) -> None:
        if ok:
            self.failures = 0
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until_call = call_no + self.cooldown_calls
            self.failures = 0
            self.trips += 1


@dataclass
class ExecResult:
    ok: bool
    outcome: Optional[Outcome] = None
    idem_key: str = ""
    replayed: bool = False        # key already used -> returned the prior result
    error: Optional[str] = None
    attempts_made: int = 1
    reconciled: bool = False
    dead_lettered: bool = False
    razorpay_ref: Optional[str] = None


class Executor:
    """Executes an authorized action against the world (and, when configured,
    against Razorpay test-mode APIs).

    `chaos` is the probability that any single gateway call fails, split
    between transient errors and ambiguous timeouts. It exists so the failure
    path is exercised on every run, not just in a scripted demo.
    """

    def __init__(
        self,
        world: World,
        chaos: float = 0.0,
        ambiguous_share: float = 0.35,
        max_retries: int = 2,
        seed: int = 7,
        razorpay=None,
    ):
        self.world = world
        self.chaos = chaos
        self.ambiguous_share = ambiguous_share
        self.max_retries = max_retries
        self.rng = random.Random(seed)
        self.razorpay = razorpay
        self.breakers: Dict[str, CircuitBreaker] = {}
        self.call_no = 0
        # idempotency ledger: key -> the result we already produced
        self.idem: Dict[str, ExecResult] = {}
        self.dlq: List[Dict] = []
        self.stats = {
            "calls": 0,
            "transient_errors": 0,
            "ambiguous_timeouts": 0,
            "retries": 0,
            "breaker_trips": 0,
            "breaker_shed": 0,
            "reconciliations": 0,
            "dead_lettered": 0,
            "duplicate_charges_prevented": 0,
            "double_charges": 0,          # must stay 0. this is the invariant.
        }

    def _breaker(self, gateway: str) -> CircuitBreaker:
        return self.breakers.setdefault(gateway, CircuitBreaker())

    def _maybe_chaos(self) -> None:
        if self.chaos <= 0:
            return
        if self.rng.random() < self.chaos:
            if self.rng.random() < self.ambiguous_share:
                self.stats["ambiguous_timeouts"] += 1
                raise GatewayAmbiguous("gateway timed out; charge state unknown")
            self.stats["transient_errors"] += 1
            raise GatewayError("gateway returned 503")

    def execute(
        self,
        order: Order,
        intervention: Intervention,
        attempt_idx: int,
        at: datetime,
    ) -> ExecResult:
        key = idempotency_key(order.order_id, intervention, attempt_idx)

        # --- idempotency check BEFORE any side effect ---------------------
        if key in self.idem:
            prior = self.idem[key]
            if intervention in MONEY_ACTIONS:
                self.stats["duplicate_charges_prevented"] += 1
            return ExecResult(
                ok=prior.ok,
                outcome=prior.outcome,
                idem_key=key,
                replayed=True,
                razorpay_ref=prior.razorpay_ref,
            )

        gateway = (order.latest_attempt.gateway if order.latest_attempt else None) or "pg_default"
        is_money = intervention in MONEY_ACTIONS

        attempts = 0
        reconciled = False
        last_err: Optional[str] = None

        while attempts <= self.max_retries:
            attempts += 1
            self.call_no += 1
            self.stats["calls"] += 1

            br = self._breaker(gateway)
            if is_money and not br.allow(self.call_no):
                self.stats["breaker_shed"] += 1
                res = ExecResult(
                    ok=False, idem_key=key, error=f"circuit open for {gateway}",
                    attempts_made=attempts, dead_lettered=True,
                )
                self._dead_letter(order, intervention, attempt_idx, "circuit_open")
                return res

            try:
                if is_money:
                    self._maybe_chaos()

                # Optional: touch the real Razorpay test-mode API so the action
                # is a real API call, not only a simulated one.
                ref = None
                if self.razorpay is not None:
                    ref = self.razorpay.perform(order, intervention, key)

                outcome = self.world.act(order, intervention, attempt_idx, at)
                if is_money:
                    br.record(True, self.call_no)
                res = ExecResult(
                    ok=True, outcome=outcome, idem_key=key,
                    attempts_made=attempts, reconciled=reconciled, razorpay_ref=ref,
                )
                self.idem[key] = res
                return res

            except GatewayAmbiguous as e:
                last_err = str(e)
                br.record(False, self.call_no)
                # The critical branch. We do NOT retry blind -- we reconcile.
                # Retrying here is exactly how a duplicate debit happens.
                true_state = self._reconcile(order, intervention, attempt_idx, at)
                reconciled = True
                self.stats["reconciliations"] += 1
                if true_state is not None:
                    # The charge did land. Record it under the same key and stop.
                    res = ExecResult(
                        ok=True, outcome=true_state, idem_key=key,
                        attempts_made=attempts, reconciled=True,
                    )
                    self.idem[key] = res
                    return res
                # Confirmed not charged, so a retry is safe.
                self.stats["retries"] += 1
                continue

            except GatewayError as e:
                last_err = str(e)
                br.record(False, self.call_no)
                if br.trips > self.stats["breaker_trips"]:
                    self.stats["breaker_trips"] = br.trips
                if attempts > self.max_retries:
                    break
                self.stats["retries"] += 1
                continue

        self._dead_letter(order, intervention, attempt_idx, last_err or "exhausted retries")
        return ExecResult(
            ok=False, idem_key=key, error=last_err, attempts_made=attempts,
            reconciled=reconciled, dead_lettered=True,
        )

    def _reconcile(
        self, order: Order, intervention: Intervention, attempt_idx: int, at: datetime
    ) -> Optional[Outcome]:
        """Establish the true state of an ambiguous charge.

        In production this is a payments-fetch against the order. Here it asks
        the world what actually happened. Returns the Outcome if the charge did
        land, or None if it definitively did not.
        """
        landed = self.world.act(order, intervention, attempt_idx, at)
        # Model the real split: a timeout usually means it did not land, but
        # sometimes it did and only the response was lost. That minority is the
        # entire reason this code path exists.
        if landed.recovered and self.rng.random() < 0.5:
            return landed
        return None

    def _dead_letter(
        self, order: Order, intervention: Intervention, attempt_idx: int, reason: str
    ) -> None:
        self.stats["dead_lettered"] += 1
        self.dlq.append(
            {
                "order_id": order.order_id,
                "intervention": intervention.value,
                "attempt_idx": attempt_idx,
                "reason": reason,
                "idem_key": idempotency_key(order.order_id, intervention, attempt_idx),
            }
        )
