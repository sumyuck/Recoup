"""Detection layer -- deliberately no LLM.

Two jobs:

1.  `detect_degradations` finds infrastructure-level revenue leaks: a
    (issuer x method x gateway) cell whose success rate has collapsed inside an
    hourly window. This is a two-proportion z-test against the cell's own
    trailing baseline, with a volume floor so a 0/2 hour cannot manufacture an
    incident.

2.  `build_risk_register` turns each failed order into a compact set of risk
    facts, including whether it sits inside a detected degradation cluster.

Why no model here: this is a hypothesis test over counts. An LLM asked "is 61%
lower than 94%" adds latency, cost, and non-determinism to arithmetic that is
exact. Using one would be the wrong tool and, more to the point, it would make
the numbers unreproducible -- which would poison the evaluation downstream.
The LLM's turn comes next, in diagnosis, where the input is unstructured text.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .models import Batch, Order, TrafficEvent

# Tunables. Every one of these is a defensible tradeoff, not a magic number:
#   MIN_BUCKET_VOLUME -- below this, a cell's hourly rate is too noisy to act on
#   MIN_BASELINE_VOLUME -- need enough history for the baseline to mean anything
#   ALPHA -- 0.01 not 0.05, because a false outage call triggers mass retries
#   MIN_ABSOLUTE_DROP -- statistical significance on a tiny effect is not worth
#                        an incident; require a materially large drop too
MIN_BUCKET_VOLUME = 25
MIN_BASELINE_VOLUME = 150
BASELINE_LOOKBACK_HOURS = 72
BUCKET_HOURS = 3
ALPHA = 0.01
MIN_ABSOLUTE_DROP = 0.20

# Multiple-comparison correction. We test (number of cells x number of buckets)
# hypotheses per run -- on the order of a few thousand. At a naive alpha of 0.01
# that alone manufactures ~30 "incidents" out of pure noise, and the first
# version of this detector did exactly that. Bonferroni is conservative but it
# is the right default when a false positive triggers mass retries against a
# bank that is actually healthy.
USE_BONFERRONI = True

# Outages present in two different geometries, so the detector slices traffic
# two ways. An issuer-side incident (one bank down) is invisible to a
# gateway-side slice because it is diluted across every PG, and vice versa.
# Each dimension is tested independently and the results are merged.
DIMENSIONS = ("issuer", "gateway")


def _norm_sf(z: float) -> float:
    """One-sided survival function of the standard normal, via erfc.

    Avoids a scipy dependency for what is one line of math.
    """
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _two_proportion_z(x1: int, n1: int, x2: int, n2: int) -> Tuple[float, float]:
    """Pooled two-proportion z-test. Returns (z, one-sided p) for p1 < p2."""
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p1, p2 = x1 / n1, x2 / n2
    p_pool = (x1 + x2) / (n1 + n2)
    denom = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if denom == 0:
        return 0.0, 1.0
    z = (p1 - p2) / denom
    return z, _norm_sf(-z)      # we only care about a DROP


@dataclass
class DegradationSignal:
    """One detected infrastructure incident."""

    signal_id: str
    dimension: str          # which slice found it: "issuer" or "gateway"
    issuer: Optional[str]
    method: str
    gateway: Optional[str]
    window_start: datetime
    window_end: datetime
    observed_success_rate: float
    baseline_success_rate: float
    observed_n: int
    baseline_n: int
    z_score: float
    p_value: float
    amount_at_risk_inr: float
    affected_order_ids: List[str] = field(default_factory=list)
    # Stamped by the caller once the total hypothesis count is known.
    alpha_used: float = ALPHA
    n_hypotheses: int = 0
    # Cross-dimension attribution (see _attribute_incidents).
    shadow_of: Optional[str] = None
    shadow_signal_ids: List[str] = field(default_factory=list)
    blast_radius: int = 1

    @property
    def cell(self) -> str:
        subject = self.issuer if self.dimension == "issuer" else self.gateway
        return f"{self.dimension}:{subject}|{self.method}"

    @property
    def drop_pp(self) -> float:
        """Drop in percentage points -- the number a human actually reads."""
        return round((self.baseline_success_rate - self.observed_success_rate) * 100, 1)

    def to_dict(self) -> Dict:
        return {
            "signal_id": self.signal_id,
            "dimension": self.dimension,
            "cell": self.cell,
            "issuer": self.issuer,
            "method": self.method,
            "gateway": self.gateway,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "observed_success_rate": round(self.observed_success_rate, 4),
            "baseline_success_rate": round(self.baseline_success_rate, 4),
            "drop_pp": self.drop_pp,
            "observed_n": self.observed_n,
            "baseline_n": self.baseline_n,
            "z_score": round(self.z_score, 3),
            "p_value": float(f"{self.p_value:.3e}"),
            "alpha_used": float(f"{self.alpha_used:.3e}"),
            "n_hypotheses": self.n_hypotheses,
            "amount_at_risk_inr": round(self.amount_at_risk_inr, 2),
            "affected_orders": len(self.affected_order_ids),
            "blast_radius_cells": self.blast_radius,
            "shadow_signal_ids": self.shadow_signal_ids,
        }


def _bucket(ts: datetime) -> datetime:
    """Floor a timestamp into a fixed bucket.

    3-hour buckets, not 1-hour: at this merchant's volume an hourly
    (cell x bucket) slice holds too few attempts to reject a null hypothesis,
    so hourly resolution would buy sensitivity we cannot actually support and
    would just produce noise. Coarser buckets, honest statistics.
    """
    floored = ts.replace(minute=0, second=0, microsecond=0)
    return floored - timedelta(hours=floored.hour % BUCKET_HOURS)


def detect_degradations(
    traffic: List[TrafficEvent],
    return_coverage: bool = False,
):
    """Scan for success-rate collapse across both cell geometries.

    Runs in two passes because the significance threshold depends on how many
    hypotheses we end up testing: pass one counts the testable (cell, bucket)
    pairs, pass two applies the corrected threshold.
    """
    candidates: List[DegradationSignal] = []
    n_tests = 0
    cov = {"testable_buckets": 0, "skipped_low_volume": 0, "skipped_no_baseline": 0}

    for dim in DIMENSIONS:
        sigs, tests, c = _detect_on_dimension(traffic, dim)
        candidates.extend(sigs)
        n_tests += tests
        for k in cov:
            cov[k] += c[k]

    threshold = (ALPHA / max(1, n_tests)) if USE_BONFERRONI else ALPHA
    kept = [s for s in candidates if s.p_value < threshold]
    for s in kept:
        s.alpha_used = threshold
        s.n_hypotheses = n_tests

    kept.sort(key=lambda s: s.window_start)
    merged = _merge_adjacent(kept)
    merged, shadows = _attribute_incidents(merged)
    if return_coverage:
        cov["cross_dimension_shadows"] = len(shadows)
        cov.update(
            {
                "hypotheses_tested": n_tests,
                "alpha_naive": ALPHA,
                "alpha_bonferroni": threshold,
                "candidates_before_correction": len(candidates),
                "signals_after_correction": len(merged),
            }
        )
        return merged, cov
    return merged


def _detect_on_dimension(traffic: List[TrafficEvent], dim: str):
    # cell -> bucket -> [successes, total]
    cells: Dict[Tuple, Dict[datetime, List[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    amounts: Dict[Tuple, Dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    orders: Dict[Tuple, Dict[datetime, List[str]]] = defaultdict(lambda: defaultdict(list))

    for t in traffic:
        key = (t.issuer, t.method) if dim == "issuer" else (t.gateway, t.method)
        b = _bucket(t.ts)
        cells[key][b][1] += 1
        if t.success:
            cells[key][b][0] += 1
        else:
            amounts[key][b] += t.amount_inr
            if t.order_id:
                orders[key][b].append(t.order_id)

    signals: List[DegradationSignal] = []
    n_tests = 0
    cov = {"testable_buckets": 0, "skipped_low_volume": 0, "skipped_no_baseline": 0}
    for key, buckets in cells.items():
        if dim == "issuer":
            issuer, method, gateway = key[0], key[1], None
        else:
            issuer, method, gateway = None, key[1], key[0]
        hours = sorted(buckets.keys())
        for h in hours:
            succ, tot = buckets[h]
            if tot < MIN_BUCKET_VOLUME:
                cov["skipped_low_volume"] += 1
                continue
            # Baseline = the same cell over the preceding lookback window.
            # Comparing a cell to ITSELF rather than to the global rate matters:
            # RuPay-on-netbanking is structurally worse than VISA-on-card, and a
            # global baseline would flag that permanently.
            lo = h - timedelta(hours=BASELINE_LOOKBACK_HOURS)
            b_succ = b_tot = 0
            for hh in hours:
                if lo <= hh < h:
                    b_succ += buckets[hh][0]
                    b_tot += buckets[hh][1]
            if b_tot < MIN_BASELINE_VOLUME:
                cov["skipped_no_baseline"] += 1
                continue

            # This pair is genuinely testable, so it counts toward the
            # multiple-comparison burden whether or not it fires.
            n_tests += 1
            cov["testable_buckets"] += 1

            obs_rate, base_rate = succ / tot, b_succ / b_tot
            if (base_rate - obs_rate) < MIN_ABSOLUTE_DROP:
                continue
            z, p = _two_proportion_z(succ, tot, b_succ, b_tot)
            # Correction is applied by the caller, which knows the final n.

            signals.append(
                DegradationSignal(
                    signal_id=f"sig_{dim}_{issuer or gateway or 'na'}_{method}_{h:%m%d%H}".lower(),
                    dimension=dim,
                    issuer=issuer,
                    method=method,
                    gateway=gateway,
                    window_start=h,
                    window_end=h + timedelta(hours=BUCKET_HOURS),
                    observed_success_rate=obs_rate,
                    baseline_success_rate=base_rate,
                    observed_n=tot,
                    baseline_n=b_tot,
                    z_score=z,
                    p_value=p,
                    amount_at_risk_inr=amounts[key][h],
                    affected_order_ids=orders[key][h],
                )
            )

    return signals, n_tests, cov


def _attribute_incidents(
    signals: List[DegradationSignal],
) -> Tuple[List[DegradationSignal], List[DegradationSignal]]:
    """Collapse cross-dimensional shadows of a single root cause.

    This is the fix for a real bug, and it is worth spelling out. Slicing
    traffic two ways means one incident can legitimately fire on both slices:

      * an ICICI outage tanks netbanking on EVERY gateway, so each gateway's
        netbanking cell also dips and fires on the gateway slice
      * a pg_alpha outage tanks netbanking for EVERY bank, so HDFC's and SBIN's
        netbanking cells also dip and fire on the issuer slice

    My first version reported all of them, so two injected outages surfaced as
    five "incidents" -- a precision of 0.4 that had nothing to do with
    statistics and everything to do with double-counting one root cause.

    The rule: among signals overlapping in time on the same method, the one
    with the largest effect size is the root cause. The rest are recorded as
    its shadows -- kept in the audit trail, excluded from the incident list, so
    on-call sees one incident with a blast radius rather than five pages.
    """
    if not signals:
        return [], []

    # Group by method, then by time overlap.
    groups: Dict[str, List[DegradationSignal]] = defaultdict(list)
    for s in signals:
        groups[s.method].append(s)

    primaries: List[DegradationSignal] = []
    shadows: List[DegradationSignal] = []

    for _, group in groups.items():
        group.sort(key=lambda s: s.window_start)
        clusters: List[List[DegradationSignal]] = []
        for s in group:
            placed = False
            for cl in clusters:
                # overlap against any member of the cluster
                if any(s.window_start < m.window_end and s.window_end > m.window_start for m in cl):
                    cl.append(s)
                    placed = True
                    break
            if not placed:
                clusters.append([s])

        for cl in clusters:
            # Effect size, not p-value: p depends on n, and the shadow cells
            # can carry more traffic than the true root-cause cell.
            root = max(cl, key=lambda s: (s.baseline_success_rate - s.observed_success_rate))
            root.shadow_signal_ids = [x.signal_id for x in cl if x is not root]
            root.blast_radius = len(cl)
            primaries.append(root)
            for x in cl:
                if x is not root:
                    x.shadow_of = root.signal_id
                    # A shadow's affected orders still belong to the incident.
                    root.affected_order_ids.extend(x.affected_order_ids)
                    root.amount_at_risk_inr += x.amount_at_risk_inr
                    shadows.append(x)

    primaries.sort(key=lambda s: -s.amount_at_risk_inr)
    return primaries, shadows


def _merge_adjacent(signals: List[DegradationSignal]) -> List[DegradationSignal]:
    """Collapse consecutive anomalous hours on the same cell into one incident.

    Without this, a 5-hour outage reports as five separate signals and the
    on-call view becomes noise. Incidents are what humans reason about, not
    hour-buckets.
    """
    by_cell: Dict[str, List[DegradationSignal]] = defaultdict(list)
    for s in signals:
        by_cell[s.cell].append(s)

    merged: List[DegradationSignal] = []
    for _, group in by_cell.items():
        group.sort(key=lambda s: s.window_start)
        cur = None
        for s in group:
            if cur is not None and s.window_start <= cur.window_end:
                # extend
                cur.window_end = s.window_end
                tot_n = cur.observed_n + s.observed_n
                cur.observed_success_rate = (
                    cur.observed_success_rate * cur.observed_n
                    + s.observed_success_rate * s.observed_n
                ) / tot_n
                cur.observed_n = tot_n
                cur.amount_at_risk_inr += s.amount_at_risk_inr
                cur.affected_order_ids.extend(s.affected_order_ids)
                cur.z_score = min(cur.z_score, s.z_score)
                cur.p_value = min(cur.p_value, s.p_value)
            else:
                if cur is not None:
                    merged.append(cur)
                cur = s
        if cur is not None:
            merged.append(cur)

    merged.sort(key=lambda s: -s.amount_at_risk_inr)
    return merged


# ---------------------------------------------------------------------------
# Order-level risk facts
# ---------------------------------------------------------------------------
@dataclass
class RiskFacts:
    """What the agent knows about one at-risk order. No ground truth in here."""

    order_id: str
    amount_inr: float
    kind: str
    age_hours: float
    attempts: int
    method: str
    issuer: Optional[str]
    gateway: Optional[str]
    error_code: Optional[str]
    error_source: Optional[str]
    error_step: Optional[str]
    error_reason: Optional[str]
    error_description: Optional[str]
    days_overdue: Optional[float] = None
    customer_prior_orders: int = 0
    customer_success_ratio: float = 0.0
    # Set when this order's failure falls inside a detected degradation window.
    in_degradation: bool = False
    degradation_signal_id: Optional[str] = None
    degradation_ends_at: Optional[datetime] = None

    def to_prompt_dict(self) -> Dict:
        """Exactly the fields handed to the diagnosis LLM -- kept explicit so
        it is obvious at review time that no hidden label leaks into the prompt."""
        d = {
            "amount_inr": self.amount_inr,
            "order_kind": self.kind,
            "age_hours": round(self.age_hours, 1),
            "failed_attempts": self.attempts,
            "method": self.method,
            "issuer": self.issuer,
            "gateway": self.gateway,
            "error_code": self.error_code,
            "error_source": self.error_source,
            "error_step": self.error_step,
            "error_reason": self.error_reason,
            "error_description": self.error_description,
            "customer_prior_orders": self.customer_prior_orders,
            "customer_success_ratio": round(self.customer_success_ratio, 2),
            "inside_detected_outage": self.in_degradation,
        }
        if self.days_overdue is not None:
            d["days_overdue"] = round(self.days_overdue, 1)
        return d


def build_risk_register(
    batch: Batch,
    signals: List[DegradationSignal],
    now: Optional[datetime] = None,
) -> Dict[str, RiskFacts]:
    now = now or batch.generated_at

    # order_id -> signal, for the outage-membership flag
    in_sig: Dict[str, DegradationSignal] = {}
    for s in signals:
        for oid in s.affected_order_ids:
            in_sig[oid] = s

    register: Dict[str, RiskFacts] = {}
    for o in batch.orders:
        a = o.latest_attempt
        cust = batch.customers[o.customer_id]
        sig = in_sig.get(o.order_id)

        # Not every revenue leak has a failed charge behind it. An abandoned
        # checkout and an overdue invoice have NO payment attempt at all, so
        # reading the error surface off `latest_attempt` yields nothing and the
        # diagnoser sees a blank record.
        #
        # This was a real bug: roughly a fifth of the corpus -- every abandoned
        # checkout and every overdue receivable -- was arriving at diagnosis
        # with error_reason=None, collapsing to UNKNOWN, and getting a single
        # generic email instead of the right playbook. A production system knows
        # perfectly well that an invoice exists and is 40 days past due, so the
        # facts are reconstructed from observable order state instead.
        derived_reason = None
        derived_step = None
        derived_source = None
        derived_desc = None
        if a is None:
            if o.kind.value == "B2B_INVOICE":
                overdue = ((now - o.due_at).total_seconds() / 86400.0) if o.due_at else 0.0
                derived_reason = "invoice_overdue"
                derived_step = "collection"
                derived_source = "business"
                derived_desc = (
                    f"Invoice of Rs{o.amount_inr:,.0f} is {overdue:.0f} days past due "
                    f"with no payment attempt recorded"
                )
            else:
                derived_reason = "checkout_abandoned"
                derived_step = "checkout"
                derived_source = "customer"
                derived_desc = (
                    "Customer created the order but never initiated a payment attempt"
                )

        register[o.order_id] = RiskFacts(
            order_id=o.order_id,
            amount_inr=o.amount_inr,
            kind=o.kind.value,
            age_hours=(now - o.created_at).total_seconds() / 3600.0,
            attempts=o.attempt_count,
            method=a.method if a else "none",
            issuer=a.issuer if a else None,
            gateway=a.gateway if a else None,
            error_code=a.error_code if a else None,
            error_source=a.error_source if a else derived_source,
            error_step=a.error_step if a else derived_step,
            error_reason=a.error_reason if a else derived_reason,
            error_description=a.error_description if a else derived_desc,
            days_overdue=((now - o.due_at).total_seconds() / 86400.0) if o.due_at else None,
            customer_prior_orders=cust.prior_orders,
            customer_success_ratio=(
                cust.prior_successful_orders / cust.prior_orders if cust.prior_orders else 0.0
            ),
            in_degradation=sig is not None,
            degradation_signal_id=sig.signal_id if sig else None,
            degradation_ends_at=sig.window_end if sig else None,
        )
    return register


def score_detection(batch: Batch, signals: List[DegradationSignal]) -> Dict:
    """Score detected incidents against the outages we actually injected.

    Reported in the results table because a detector that finds three incidents
    when two exist is a different system from one that finds two.
    """
    truth = batch.injected_outages
    matched_truth = set()
    tp = 0
    for s in signals:
        hit = False
        for i, og in enumerate(truth):
            subject_match = (
                s.issuer == og["issuer"] if og["kind"] == "issuer" else s.gateway == og["gateway"]
            )
            if (
                s.dimension == og["kind"]
                and subject_match
                and s.method == og["method"]
                and s.window_start <= datetime.fromisoformat(og["end"])
                and s.window_end >= datetime.fromisoformat(og["start"])
            ):
                hit = True
                matched_truth.add(i)
        tp += 1 if hit else 0
    fp = len(signals) - tp
    fn = len(truth) - len(matched_truth)
    return {
        "injected_outages": len(truth),
        "signals_raised": len(signals),
        "true_positives": tp,
        "false_positives": fp,
        "missed_outages": fn,
        "precision": round(tp / len(signals), 3) if signals else None,
        "recall": round(len(matched_truth) / len(truth), 3) if truth else None,
    }
