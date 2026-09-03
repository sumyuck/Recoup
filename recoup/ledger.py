"""Append-only, hash-chained decision ledger.

Every state change in Recoup lands here -- signals, diagnoses, proposals,
policy verdicts (allow AND deny), executions, outcomes, and stops. Nothing
mutates money or contacts a human without first writing an entry.

Design notes worth defending:

*   **Hash chain, not just a log.** Each entry stores the hash of the previous
    entry, and its own hash covers that link. Editing or deleting any historical
    entry invalidates every hash after it, and `verify()` reports the exact
    sequence number where the chain broke. An audit log you can silently edit is
    not evidence.

*   **JSONL is the source of truth**, not a database. The whole run is
    reconstructible by replaying the file top to bottom, which is what makes
    "why did we call this customer at 10:14?" answerable months later.

*   **Decision inputs are snapshotted**, not referenced. Storing an order_id and
    looking it up later tells you what the order looks like *now*; storing the
    snapshot tells you what the agent actually saw when it decided.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

GENESIS = "0" * 64


class EventType(str, Enum):
    BATCH_OPEN = "BATCH_OPEN"
    BUDGET_PLANNED = "BUDGET_PLANNED"        # shadow price solved for the batch
    SIGNAL_RAISED = "SIGNAL_RAISED"          # detection found revenue at risk
    DIAGNOSIS = "DIAGNOSIS"                  # root cause determined
    ACTION_PROPOSED = "ACTION_PROPOSED"      # agent wants to do something
    POLICY_VERDICT = "POLICY_VERDICT"        # gate said allow / deny
    ACTION_EXECUTED = "ACTION_EXECUTED"      # it actually happened
    ACTION_FAILED = "ACTION_FAILED"          # execution error, with handling
    OUTCOME_OBSERVED = "OUTCOME_OBSERVED"    # recovered / still failed
    PROMISE_TO_PAY = "PROMISE_TO_PAY"        # customer committed to a date
    OPT_OUT = "OPT_OUT"                      # permanent suppression
    SEQUENCE_STOPPED = "SEQUENCE_STOPPED"    # a stopping rule fired
    HUMAN_ESCALATED = "HUMAN_ESCALATED"      # parked for approval
    BUDGET_HALT = "BUDGET_HALT"              # batch spend cap hit
    RECONCILE = "RECONCILE"                  # true state confirmed after error
    BATCH_CLOSE = "BATCH_CLOSE"


class Actor(str, Enum):
    DETECTOR = "DETECTOR"        # deterministic statistics
    DIAGNOSER = "DIAGNOSER"      # the LLM
    POLICY = "POLICY"            # the deterministic gate
    EXECUTOR = "EXECUTOR"        # side-effecting layer
    SIMULATOR = "SIMULATOR"      # synthetic customer/world
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


def _canonical(obj: Any) -> str:
    """Stable serialisation. Sorted keys + no whitespace variance, otherwise the
    same logical entry could hash two different ways."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(entry: Dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


class Ledger:
    """Thread-safe append-only ledger backed by a JSONL file."""

    def __init__(self, path: str, run_id: str, policy_version: int):
        self.path = path
        self.run_id = run_id
        self.policy_version = policy_version
        self._seq = 0
        self._prev = GENESIS
        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = []
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Fresh file per run; history lives in artifacts/runs/<run_id>/.
        with open(self.path, "w") as fh:
            fh.write("")

    # -- writing ------------------------------------------------------------
    def append(
        self,
        event: EventType,
        actor: Actor,
        *,
        order_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        model_version: Optional[str] = None,
        prompt_hash: Optional[str] = None,
        cost_inr: float = 0.0,
        arm: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry: Dict[str, Any] = {
                "seq": self._seq,
                "run_id": self.run_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event.value,
                "actor": actor.value,
                "order_id": order_id,
                "customer_id": customer_id,
                "arm": arm,
                # Stamping the policy version means a historical decision can be
                # replayed against the exact rules that produced it.
                "policy_version": self.policy_version,
                "model_version": model_version,
                "prompt_hash": prompt_hash,
                "cost_inr": round(cost_inr, 4),
                "payload": payload or {},
                "prev_hash": self._prev,
            }
            entry["entry_hash"] = entry_hash(entry)
            self._prev = entry["entry_hash"]
            self._entries.append(entry)
            with open(self.path, "a") as fh:
                fh.write(_canonical(entry) + "\n")
            return entry

    # -- reading ------------------------------------------------------------
    @property
    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def for_order(self, order_id: str) -> List[Dict[str, Any]]:
        """The full decision trail for one order -- this is the view that makes
        the system auditable to a human."""
        return [e for e in self._entries if e.get("order_id") == order_id]

    def total_cost(self) -> float:
        return round(sum(e.get("cost_inr", 0.0) for e in self._entries), 2)

    def count(self, event: EventType) -> int:
        return sum(1 for e in self._entries if e["event"] == event.value)

    # -- integrity ----------------------------------------------------------
    def verify(self) -> Dict[str, Any]:
        """Walk the chain and confirm nothing has been altered.

        Returns the first break rather than a bare boolean, because "the log is
        invalid" is useless without "at entry 1,204, and here is what changed".
        """
        prev = GENESIS
        for e in self._entries:
            if e["prev_hash"] != prev:
                return {
                    "valid": False,
                    "broken_at_seq": e["seq"],
                    "reason": "prev_hash does not match preceding entry",
                    "expected_prev": prev,
                    "found_prev": e["prev_hash"],
                }
            recomputed = entry_hash(e)
            if recomputed != e["entry_hash"]:
                return {
                    "valid": False,
                    "broken_at_seq": e["seq"],
                    "reason": "entry contents do not match its hash (tampered)",
                    "expected_hash": recomputed,
                    "found_hash": e["entry_hash"],
                }
            prev = e["entry_hash"]
        return {"valid": True, "entries": len(self._entries), "head": prev}


def verify_file(path: str) -> Dict[str, Any]:
    """Verify a ledger file on disk without needing the run that produced it."""
    prev = GENESIS
    n = 0
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            n += 1
            if e["prev_hash"] != prev:
                return {"valid": False, "broken_at_seq": e.get("seq"), "reason": "chain break"}
            if entry_hash(e) != e["entry_hash"]:
                return {"valid": False, "broken_at_seq": e.get("seq"), "reason": "tampered entry"}
            prev = e["entry_hash"]
    return {"valid": True, "entries": n, "head": prev}


def replay(path: str) -> Iterable[Dict[str, Any]]:
    """Stream a ledger back in order. The run is a pure function of this file."""
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
