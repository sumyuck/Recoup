"""Synthetic corpus generator.

Three things make this more than `random.choice`:

1.  Every order carries a hidden `GroundTruth` with a `self_heal_prob` and a
    per-intervention `responsiveness` map. That is what lets the evaluation
    report incremental lift and a recovery ceiling instead of a bare rate.

2.  The error surface is shaped like Razorpay's actual failure payloads
    (`error_code` / `error_source` / `error_step` / `error_reason`), because the
    diagnosis LLM reads that raw text and we want the task to be the real one.

3.  Failures are not i.i.d. Issuer outages are injected as time-localised
    spikes on a specific (issuer, method, gateway) cell, so the detection layer
    has a genuine anomaly to find rather than a flat background it can only
    threshold against.
"""
from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .models import (
    Batch,
    TrafficEvent,
    CustomerFlag,
    CustomerView,
    FailedAttempt,
    FailureClass,
    GroundTruth,
    Intervention,
    Order,
    OrderKind,
)

I = Intervention

# ---------------------------------------------------------------------------
# Global realism scaling on intervention effectiveness.
#
# The per-class `responsiveness` ranges below were my first estimate and they
# were far too kind. Unscaled, they produced a +42pp incremental lift and had
# the agent capturing 99% of the theoretical recovery ceiling -- a number that
# should be distrusted on sight. Published dunning and retry benchmarks put
# *incremental* lift in the 5-20pp band; anything near 40 means the simulated
# world is rolling over, not that the agent is good.
#
# The relative ordering of actions is the part the agent has to learn and that
# ordering is what the ranges encode, so scaling them all by a single constant
# preserves the learning problem while making the achievable outcome realistic.
# ---------------------------------------------------------------------------
REALISM_SCALE = 0.52

# Share of B2B receivables that are simply not collectable by dunning --
# disputed, insolvent, or stuck behind a procurement process no nudge will move.
# Without this the invoice book is uniformly winnable, which no receivables
# ledger ever is.
B2B_UNCOLLECTABLE_SHARE = 0.18

# ---------------------------------------------------------------------------
# Field noise -- and why this exists.
#
# With a clean corpus, diagnosis hit 100% on BOTH tiers. That is not a good
# result either: it means every failure is fully determined by its structured
# `error_reason`, so the LLM is doing work a dict lookup already does, and
# arm C can never beat arm B. The clean corpus cannot answer "does the model
# add anything".
#
# Real payment failures are not that tidy. Gateways omit `error_reason`
# entirely, return vendor-specific codes that are in nobody's taxonomy,
# misattribute `error_source` during an incident, and put the only usable
# signal in a free-text `error_description` written by whoever built the
# integration. That is exactly the input a language model should beat a lookup
# table on -- and if it doesn't, the honest answer is that the model isn't
# earning its place.
#
# So `noise` degrades the structured fields while PRESERVING the truth in the
# free text, and the two tiers are scored on the same rows.
# ---------------------------------------------------------------------------

# Vendor-specific codes that map to nothing in the deterministic taxonomy.
VENDOR_CODES: Dict[FailureClass, List[str]] = {
    FailureClass.ISSUER_DOWN: ["GW_5023", "ISSUER_UNAVAILABLE_RETRY", "BANK_DOWNTIME_02"],
    FailureClass.GATEWAY_TIMEOUT: ["ETIMEDOUT_UPSTREAM", "GW_TIMEOUT_504"],
    FailureClass.INSUFFICIENT_FUNDS: ["NPCI_U31", "DECLINE_51", "ACCT_BAL_LOW"],
    FailureClass.AUTH_3DS_TIMEOUT: ["ACS_NO_RESPONSE", "3DS_ABANDON", "OTP_WINDOW_CLOSED"],
    FailureClass.CARD_EXPIRED: ["DECLINE_54", "CARD_EXP_INVALID"],
    FailureClass.DO_NOT_HONOR: ["DECLINE_05", "DNH_GENERIC"],
    FailureClass.UPI_COLLECT_EXPIRED: ["NPCI_U69", "COLLECT_TTL_EXPIRED"],
    FailureClass.MANDATE_REVOKED: ["MANDATE_NOT_FOUND", "NACH_REVOKED"],
    FailureClass.MANDATE_INSUFFICIENT: ["NACH_RET_01", "ACH_R01"],
    FailureClass.RISK_BLOCKED: ["RISK_HOLD_9", "FRM_BLOCK"],
}

# Free-text descriptions that carry the real cause in prose. This is the signal
# a model can read and a lookup table cannot.
NOISY_DESCRIPTIONS: Dict[FailureClass, List[str]] = {
    FailureClass.ISSUER_DOWN: [
        "issuer host unreachable during scheduled bank maintenance window, advise retry after some time",
        "upstream bank switch returned no response for this BIN; multiple merchants affected",
    ],
    FailureClass.GATEWAY_TIMEOUT: [
        "no response received from processor within configured socket timeout, txn state indeterminate",
        "request abandoned after upstream read timeout; no auth code returned",
    ],
    FailureClass.INSUFFICIENT_FUNDS: [
        "customer account did not have sufficient balance at time of debit; salary credit pending",
        "available balance below txn amount, customer asked to fund account and retry",
    ],
    FailureClass.AUTH_3DS_TIMEOUT: [
        "cardholder did not complete second factor on the issuer ACS page before it expired",
        "customer closed the OTP screen without submitting, authentication incomplete",
    ],
    FailureClass.CARD_EXPIRED: [
        "instrument validity period has elapsed, customer must supply a current card",
        "stored credential is past its expiry date and cannot be charged",
    ],
    FailureClass.DO_NOT_HONOR: [
        "issuer refused authorization without assigning a specific reason code",
        "bank declined at their discretion, customer advised to contact card issuer",
    ],
    FailureClass.UPI_COLLECT_EXPIRED: [
        "collect request lapsed before the payer approved it in their UPI application",
        "payer never acted on the pending mandate request, request auto-cancelled",
    ],
    FailureClass.MANDATE_REVOKED: [
        "no active debit authorization exists for this subscriber, mandate was cancelled earlier",
        "standing instruction has been withdrawn by the account holder",
    ],
    FailureClass.MANDATE_INSUFFICIENT: [
        "presentation against an active mandate was returned unpaid by the drawee bank for want of funds",
        "auto debit on live standing instruction bounced at the bank for low balance",
    ],
    FailureClass.RISK_BLOCKED: [
        "transaction stopped by internal fraud rules before reaching the network",
        "blocked pre-authorization by risk engine, do not retry",
    ],
}

ISSUERS = ["HDFC", "ICICI", "SBIN", "AXIS", "KOTAK", "PAYTM", "YESB", "IDFC"]
ISSUER_WEIGHTS = [22, 18, 20, 12, 8, 7, 7, 6]
GATEWAYS = ["pg_alpha", "pg_beta", "pg_gamma"]
NETWORKS = ["VISA", "MASTERCARD", "RUPAY"]


@dataclass
class ClassProfile:
    """Everything needed to mint a realistic failure of one class."""

    weight: float
    methods: List[str]
    error_code: str
    error_source: str
    error_step: str
    error_reason: str
    descriptions: List[str]
    self_heal: Tuple[float, float]          # uniform range
    responsiveness: Dict[Intervention, Tuple[float, float]]
    kinds: List[OrderKind] = field(default_factory=lambda: [OrderKind.ONE_TIME])
    amount_range: Tuple[float, float] = (199.0, 12000.0)
    unrecoverable: bool = False


# ---------------------------------------------------------------------------
# The taxonomy, with honest priors.
#
# `self_heal` is the single most important number here. Transient failures heal
# themselves at a high rate -- any system claiming credit for those without a
# holdout is reporting fiction. Dead instruments never heal, so all of their
# recovery is genuinely incremental.
# ---------------------------------------------------------------------------
PROFILES: Dict[FailureClass, ClassProfile] = {
    FailureClass.ISSUER_DOWN: ClassProfile(
        weight=11,
        methods=["card", "netbanking", "upi"],
        error_code="GATEWAY_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="gateway_technical_error",
        descriptions=[
            "Payment processing failed because of an error at bank's end",
            "Bank server was unavailable, please retry",
        ],
        # Outages end. Most of these come back by themselves.
        self_heal=(0.55, 0.75),
        responsiveness={
            I.RETRY_SCHEDULED: (0.72, 0.88),
            I.RETRY_NOW: (0.20, 0.35),      # retrying mid-outage mostly fails
            I.METHOD_SWITCH_LINK: (0.55, 0.70),
            I.NUDGE_SMS: (0.30, 0.42),
            I.NUDGE_WHATSAPP: (0.34, 0.46),
        },
    ),
    FailureClass.GATEWAY_TIMEOUT: ClassProfile(
        weight=8,
        methods=["card", "upi", "netbanking"],
        error_code="GATEWAY_ERROR",
        error_source="gateway",
        error_step="payment_response",
        error_reason="gateway_timeout",
        descriptions=[
            "Payment was not completed on time, please try again",
            "The payment request timed out awaiting a response",
        ],
        self_heal=(0.45, 0.62),
        responsiveness={
            I.RETRY_NOW: (0.66, 0.80),
            I.RETRY_SCHEDULED: (0.60, 0.74),
            I.NUDGE_SMS: (0.28, 0.38),
        },
    ),
    FailureClass.INSUFFICIENT_FUNDS: ClassProfile(
        weight=17,
        methods=["card", "upi", "netbanking", "emandate"],
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_authorization",
        error_reason="insufficient_funds",
        descriptions=[
            "Your payment failed as the account balance is insufficient",
            "Transaction declined due to insufficient balance",
        ],
        # Some of these self-heal on payday without anyone doing anything.
        self_heal=(0.18, 0.32),
        responsiveness={
            I.RETRY_SCHEDULED: (0.44, 0.60),   # payday-aware timing wins here
            I.RETRY_NOW: (0.08, 0.16),         # brute-forcing an empty account
            I.NUDGE_SMS: (0.32, 0.44),
            I.NUDGE_WHATSAPP: (0.38, 0.50),
            I.METHOD_SWITCH_LINK: (0.40, 0.52),
            I.VOICE_CALL: (0.48, 0.62),
        },
        kinds=[OrderKind.ONE_TIME, OrderKind.SUBSCRIPTION],
    ),
    FailureClass.AUTH_3DS_TIMEOUT: ClassProfile(
        weight=13,
        methods=["card"],
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="payment_authentication_failed",
        descriptions=[
            "Payment failed because the OTP was not entered in time",
            "3DS authentication was not completed by the customer",
        ],
        # The customer walked away; nothing heals this on its own.
        self_heal=(0.06, 0.14),
        responsiveness={
            I.METHOD_SWITCH_LINK: (0.52, 0.66),   # UPI dodges 3DS entirely
            I.NUDGE_WHATSAPP: (0.46, 0.58),
            I.NUDGE_SMS: (0.38, 0.50),
            I.RETRY_NOW: (0.10, 0.18),            # nobody is there to type an OTP
            I.NUDGE_EMAIL: (0.20, 0.30),
        },
    ),
    FailureClass.CARD_EXPIRED: ClassProfile(
        weight=7,
        methods=["card", "emandate"],
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_initiation",
        error_reason="invalid_card_expiry",
        descriptions=[
            "The card has expired, please use a different card",
            "Card expiry date is invalid or in the past",
        ],
        self_heal=(0.02, 0.06),
        responsiveness={
            I.UPDATE_INSTRUMENT_LINK: (0.50, 0.64),
            I.NUDGE_WHATSAPP: (0.40, 0.52),
            I.NUDGE_SMS: (0.32, 0.44),
            I.VOICE_CALL: (0.46, 0.58),
            # deliberately absent: RETRY_* can never work on a dead card
        },
        kinds=[OrderKind.ONE_TIME, OrderKind.SUBSCRIPTION],
    ),
    FailureClass.DO_NOT_HONOR: ClassProfile(
        weight=9,
        methods=["card"],
        error_code="BAD_REQUEST_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="payment_declined_by_bank",
        descriptions=[
            "Your payment was declined by the bank, please contact your bank",
            "Issuer declined the transaction without a specific reason",
        ],
        # Opaque issuer decline. Mostly a wall -- this is exception-list fodder.
        self_heal=(0.08, 0.16),
        responsiveness={
            I.METHOD_SWITCH_LINK: (0.30, 0.42),
            I.NUDGE_WHATSAPP: (0.16, 0.26),
            I.VOICE_CALL: (0.22, 0.32),
            I.RETRY_SCHEDULED: (0.10, 0.18),
        },
    ),
    FailureClass.UPI_COLLECT_EXPIRED: ClassProfile(
        weight=10,
        methods=["upi"],
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_authorization",
        error_reason="upi_collect_request_expired",
        descriptions=[
            "The UPI collect request expired before it was approved",
            "Customer did not approve the mandate request in the UPI app",
        ],
        self_heal=(0.22, 0.36),
        responsiveness={
            I.RETRY_NOW: (0.54, 0.68),       # fresh collect request
            I.NUDGE_WHATSAPP: (0.50, 0.64),
            I.NUDGE_SMS: (0.40, 0.52),
        },
    ),
    FailureClass.MANDATE_REVOKED: ClassProfile(
        weight=6,
        methods=["emandate", "upi"],
        error_code="BAD_REQUEST_ERROR",
        error_source="customer",
        error_step="payment_initiation",
        error_reason="mandate_revoked",
        descriptions=[
            "The mandate has been revoked by the customer",
            "Autopay mandate is no longer active for this subscription",
        ],
        self_heal=(0.01, 0.04),
        responsiveness={
            I.MANDATE_REAUTH_LINK: (0.38, 0.52),
            I.NUDGE_WHATSAPP: (0.30, 0.42),
            I.VOICE_CALL: (0.40, 0.54),
        },
        kinds=[OrderKind.SUBSCRIPTION],
        amount_range=(299.0, 4999.0),
    ),
    FailureClass.MANDATE_INSUFFICIENT: ClassProfile(
        weight=6,
        methods=["emandate"],
        error_code="BAD_REQUEST_ERROR",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="insufficient_funds",
        descriptions=[
            "Auto-debit bounced because the account balance was insufficient",
            "Mandate debit failed at the bank due to low balance",
        ],
        self_heal=(0.14, 0.26),
        responsiveness={
            I.MANDATE_REPRESENT: (0.48, 0.62),   # re-present on a smarter date
            I.NUDGE_WHATSAPP: (0.34, 0.46),
            I.NUDGE_SMS: (0.28, 0.40),
        },
        kinds=[OrderKind.SUBSCRIPTION],
        amount_range=(299.0, 6999.0),
    ),
    FailureClass.RISK_BLOCKED: ClassProfile(
        weight=4,
        methods=["card", "upi"],
        error_code="BAD_REQUEST_ERROR",
        error_source="internal",
        error_step="payment_initiation",
        error_reason="payment_blocked_risk",
        descriptions=[
            "Payment was blocked by the risk engine",
            "Transaction flagged for suspected fraud and stopped",
        ],
        self_heal=(0.0, 0.0),
        responsiveness={},          # nothing may touch these
        unrecoverable=True,
    ),
    FailureClass.CHECKOUT_ABANDONED: ClassProfile(
        weight=9,
        methods=["none"],
        error_code="",
        error_source="customer",
        error_step="checkout",
        error_reason="checkout_abandoned",
        descriptions=[
            "Customer reached the payment screen but never initiated payment",
            "Checkout session expired with no payment attempt",
        ],
        self_heal=(0.10, 0.20),
        responsiveness={
            I.METHOD_SWITCH_LINK: (0.34, 0.46),
            I.NUDGE_WHATSAPP: (0.40, 0.52),
            I.NUDGE_EMAIL: (0.22, 0.32),
            I.NUDGE_SMS: (0.30, 0.40),
        },
    ),
    FailureClass.INVOICE_OVERDUE: ClassProfile(
        weight=10,
        methods=["netbanking", "upi", "none"],
        error_code="",
        error_source="business",
        error_step="collection",
        error_reason="invoice_overdue",
        descriptions=[
            "B2B invoice is past its due date with no payment received",
            "Receivable outstanding beyond agreed credit period",
        ],
        self_heal=(0.12, 0.24),
        responsiveness={
            I.NUDGE_EMAIL: (0.26, 0.36),
            I.NUDGE_WHATSAPP: (0.36, 0.48),
            I.VOICE_CALL: (0.52, 0.68),        # B2B collections respond to voice
            # A human collections call is the strongest single lever on an
            # overdue receivable, but one call does not clear a 45-day-old
            # invoice most of the time. 0.62-0.76 was fantasy.
            I.HUMAN_COLLECTIONS_CALL: (0.44, 0.58),
            I.METHOD_SWITCH_LINK: (0.30, 0.40),
        },
        kinds=[OrderKind.B2B_INVOICE],
        amount_range=(12000.0, 150000.0),
    ),
}


def _pick(rng: random.Random, weighted: Dict[FailureClass, ClassProfile]) -> FailureClass:
    keys = list(weighted.keys())
    return rng.choices(keys, weights=[weighted[k].weight for k in keys], k=1)[0]


def _amount(rng: random.Random, lo: float, hi: float) -> float:
    """Log-uniform, because real payment amounts are heavily right-skewed and a
    uniform draw would make the high-value tail unrealistically fat."""
    import math

    v = math.exp(rng.uniform(math.log(lo), math.log(hi)))
    return round(v, 2) if v < 1000 else float(round(v, -1))


def _holdout_rank(order_id: str, seed: int) -> int:
    """Stable pseudo-random rank for an order, used to slice the holdout."""
    h = hashlib.sha256(f"{order_id}:{seed}:holdout".encode()).hexdigest()
    return int(h[:16], 16)


def assign_stratified_holdout(
    orders: List[Order],
    truth: Dict[str, GroundTruth],
    seed: int,
    holdout_pct: int,
) -> None:
    """Assign the holdout arm, stratified by (order kind x failure class).

    A plain per-order coin flip was the first thing I built and it was wrong:
    B2B invoices are ~30x the value of a typical order, so an unstratified
    holdout could land 6 or 16 of them by luck and the headline lift number
    would swing by lakhs on sampling noise alone.

    Stratifying by kind AND true failure class also balances self-heal rate
    across arms, which is the variable that most directly biases lift. Within
    each stratum, orders are ranked by a stable hash and the first
    `holdout_pct`% are held out -- deterministic, exactly proportional, and
    identical on every replay.
    """
    strata: Dict[tuple, List[Order]] = {}
    for o in orders:
        key = (o.kind.value, truth[o.order_id].true_class.value)
        strata.setdefault(key, []).append(o)

    for key, group in strata.items():
        group.sort(key=lambda o: _holdout_rank(o.order_id, seed))
        # round() not floor(), so small strata still contribute to the holdout
        n_hold = int(round(len(group) * holdout_pct / 100.0))
        for idx, o in enumerate(group):
            o.is_holdout = idx < n_hold


def _in_outage(og: Dict, ts, issuer, method, gateway) -> bool:
    """Does this attempt fall inside the blast radius of an injected outage?"""
    if not (og["start"] <= ts <= og["end"]) or method != og["method"]:
        return False
    if og["kind"] == "issuer":
        return issuer == og["issuer"]
    return gateway == og["gateway"]


def generate_batch(
    n_orders: int = 500,
    seed: int = 20260903,
    holdout_pct: int = 20,
    window_days: int = 14,
    traffic_per_hour: int = 600,
    noise: float = 0.0,
    now: Optional[datetime] = None,
) -> Batch:
    rng = random.Random(seed)
    now = now or datetime(2026, 9, 1, 10, 0, 0)
    start = now - timedelta(days=window_days)

    # --- inject outages so detection has real signal to find -----------------
    # Two different failure geometries, because they present differently and a
    # detector that only slices one way will miss the other:
    #   issuer outage  -> one bank fails across EVERY gateway
    #   gateway outage -> one PG fails for EVERY bank
    outages: List[Dict] = []
    for kind in ("issuer", "gateway"):
        method = rng.choice(["card", "netbanking", "upi"])
        o_start = start + timedelta(hours=rng.randint(24, window_days * 24 - 24))
        outages.append(
            {
                "kind": kind,
                "issuer": rng.choice(ISSUERS[:5]) if kind == "issuer" else None,
                "gateway": rng.choice(GATEWAYS) if kind == "gateway" else None,
                "method": method,
                "start": o_start,
                "end": o_start + timedelta(hours=rng.choice([4, 6, 9])),
                "severity": rng.uniform(0.72, 0.9),
            }
        )


    customers: Dict[str, CustomerView] = {}
    orders: List[Order] = []
    truth: Dict[str, GroundTruth] = {}

    # A pool of customers smaller than the order count, so frequency caps and
    # per-customer contact history actually bind on some of them.
    n_customers = max(1, int(n_orders * 0.72))
    for i in range(n_customers):
        cid = f"cust_{i:05d}"
        prior = rng.randint(0, 24)
        flags: List[CustomerFlag] = []
        if rng.random() < 0.03:
            flags.append(CustomerFlag.IN_DISPUTE)
        if rng.random() < 0.02:
            flags.append(CustomerFlag.CHARGEBACK_OPEN)
        if rng.random() < 0.01:
            flags.append(CustomerFlag.LEGAL_HOLD)
        dnd = rng.random() < 0.12
        if dnd:
            flags.append(CustomerFlag.DND_REGISTERED)
        customers[cid] = CustomerView(
            customer_id=cid,
            consent_whatsapp=rng.random() < 0.62,
            consent_voice=rng.random() < 0.55,
            dnd_registered=dnd,
            opted_out=rng.random() < 0.04,
            flags=flags,
            prior_orders=prior,
            prior_successful_orders=max(0, prior - rng.randint(0, 4)),
            locale="hi-IN" if rng.random() < 0.45 else "en-IN",
        )
    cust_ids = list(customers.keys())

    for n in range(n_orders):
        fc = _pick(rng, PROFILES)
        prof = PROFILES[fc]
        kind = rng.choice(prof.kinds)
        created = start + timedelta(minutes=rng.randint(0, window_days * 24 * 60))

        # An active outage overrides the drawn class -- this is how the
        # time-localised spike gets into the data.
        method = rng.choice(prof.methods)
        issuer = rng.choices(ISSUERS, weights=ISSUER_WEIGHTS, k=1)[0]
        gateway = rng.choice(GATEWAYS)
        for og in outages:
            # Force the attempt into the outage cell, then mark it degraded.
            cand_issuer = og["issuer"] or issuer
            cand_gw = og["gateway"] or gateway
            if og["start"] <= created <= og["end"] and rng.random() < 0.45:
                fc = FailureClass.ISSUER_DOWN
                prof = PROFILES[fc]
                issuer, method, gateway = cand_issuer, og["method"], cand_gw
                kind = OrderKind.ONE_TIME
                break

        oid = f"order_{uuid.UUID(int=rng.getrandbits(128)).hex[:14]}"
        amount = _amount(rng, *prof.amount_range)

        due = None
        if kind == OrderKind.B2B_INVOICE:
            due = created - timedelta(days=rng.randint(3, 65))
        elif kind == OrderKind.SUBSCRIPTION:
            due = created

        order = Order(
            order_id=oid,
            customer_id=rng.choice(cust_ids),
            kind=kind,
            amount_inr=amount,
            created_at=created,
            due_at=due,
            description={
                OrderKind.ONE_TIME: "Marketplace order",
                OrderKind.SUBSCRIPTION: "Monthly plan renewal",
                OrderKind.B2B_INVOICE: "Services invoice",
            }[kind],
        )

        # --- field noise -----------------------------------------------------
        # Degrade the structured fields, keep the cause recoverable from prose.
        noisy = rng.random() < noise
        noise_mode = None
        n_reason = prof.error_reason
        n_source = prof.error_source
        n_desc_pool = prof.descriptions
        if noisy and fc in NOISY_DESCRIPTIONS:
            mode = rng.choices(["drop", "vendor", "misattribute"], weights=[40, 40, 20], k=1)[0]
            noise_mode = mode
            n_desc_pool = NOISY_DESCRIPTIONS[fc]
            if mode == "drop":
                # Gateway sent no machine-readable reason at all.
                n_reason = None
            elif mode == "vendor":
                # A code from the acquirer's own namespace, in no taxonomy.
                n_reason = rng.choice(VENDOR_CODES[fc])
            else:
                # Banks do misattribute during incidents: a real issuer outage
                # reported as though the customer declined it.
                n_reason = None
                n_source = rng.choice(["gateway", "internal", "customer", "bank"])

        # 1..3 pre-existing organic attempts (not agent-initiated).
        n_attempts = 0 if fc in (FailureClass.CHECKOUT_ABANDONED, FailureClass.INVOICE_OVERDUE) else rng.choices([1, 2, 3], weights=[70, 22, 8])[0]
        for a in range(n_attempts):
            order.attempts.append(
                FailedAttempt(
                    attempt_id=f"pay_{uuid.UUID(int=rng.getrandbits(128)).hex[:14]}",
                    order_id=oid,
                    attempted_at=created + timedelta(minutes=a * rng.randint(3, 90)),
                    method=method,
                    issuer=issuer if method != "none" else None,
                    gateway=gateway if method != "none" else None,
                    network=rng.choice(NETWORKS) if method == "card" else None,
                    error_code=prof.error_code or None,
                    error_source=n_source,
                    error_step=prof.error_step,
                    error_reason=n_reason,
                    error_description=rng.choice(n_desc_pool),
                    was_agent_initiated=False,
                )
            )

        intent = min(1.0, max(0.0, rng.betavariate(2.2, 2.0)))
        resp = {k.value: round(rng.uniform(*v), 4) for k, v in prof.responsiveness.items()}
        # Low-intent customers respond worse to everything; scale the whole map,
        # then apply the global realism factor.
        resp = {
            k: round(v * (0.55 + 0.75 * intent) * REALISM_SCALE, 4)
            for k, v in resp.items()
        }

        unrec = prof.unrecoverable
        if kind == OrderKind.B2B_INVOICE and rng.random() < B2B_UNCOLLECTABLE_SHARE:
            # Genuinely uncollectable. Leave it in the corpus so it shows up on
            # the exception list rather than quietly inflating the ceiling.
            unrec = True
            resp = {}

        truth[oid] = GroundTruth(
            order_id=oid,
            true_class=fc,
            self_heal_prob=round(rng.uniform(*prof.self_heal) * (0.6 + 0.8 * intent), 4),
            responsiveness=resp,
            intent_strength=round(intent, 4),
            unrecoverable=unrec,
            field_noise_applied=bool(noise_mode),
            noise_mode=noise_mode,
        )
        orders.append(order)

    # --- background traffic ------------------------------------------------
    # Successful payments, so the detector can work on success *rate* per cell
    # rather than raw failure counts. Inside an injected outage window the
    # affected cell's success rate is crushed, which is the anomaly to find.
    traffic: List[TrafficEvent] = []
    for o in orders:
        a = o.latest_attempt
        if a is not None:
            traffic.append(
                TrafficEvent(
                    ts=a.attempted_at, method=a.method, issuer=a.issuer,
                    gateway=a.gateway, success=False, amount_inr=o.amount_inr,
                    order_id=o.order_id,
                )
            )

    # Volume matters here and I got this wrong on the first pass. Detection
    # slices traffic into (cell x time bucket); at a few thousand events the
    # cells are so sparse that no bucket clears a volume floor and the detector
    # silently finds nothing. `traffic_per_hour` is set to a mid-size merchant's
    # real throughput so the statistics have something to bite on.
    total_traffic = int(traffic_per_hour * window_days * 24)
    for _ in range(total_traffic):
        ts = start + timedelta(seconds=rng.randint(0, window_days * 24 * 3600))
        method = rng.choices(["card", "upi", "netbanking", "wallet"], weights=[36, 46, 12, 6], k=1)[0]
        issuer = rng.choices(ISSUERS, weights=ISSUER_WEIGHTS, k=1)[0]
        gw = rng.choice(GATEWAYS)
        # Baseline health varies by cell -- RuPay/netbanking is structurally
        # worse than UPI. This is why the detector baselines each cell against
        # itself instead of against a global rate.
        base_fail = {"card": 0.075, "upi": 0.045, "netbanking": 0.11, "wallet": 0.06}[method]
        success = rng.random() > base_fail
        for og in outages:
            if _in_outage(og, ts, issuer, method, gw) and rng.random() < og["severity"]:
                success = False
        traffic.append(
            TrafficEvent(ts=ts, method=method, issuer=issuer, gateway=gw,
                         success=success, amount_inr=_amount(rng, 199.0, 9000.0))
        )
    traffic.sort(key=lambda t: t.ts)

    assign_stratified_holdout(orders, truth, seed, holdout_pct)
    orders.sort(key=lambda o: o.created_at)
    return Batch(
        batch_id=f"batch_{seed}_{n_orders}",
        generated_at=now,
        seed=seed,
        orders=orders,
        customers=customers,
        ground_truth=truth,
        traffic=traffic,
        injected_outages=[
            {
                "kind": og["kind"], "issuer": og["issuer"], "method": og["method"],
                "gateway": og["gateway"], "start": og["start"].isoformat(),
                "end": og["end"].isoformat(), "severity": round(og["severity"], 3),
            }
            for og in outages
        ],
    )
