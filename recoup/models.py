"""Domain models for Recoup.

A deliberate split runs through this file: what the AGENT is allowed to see
(`Order`, `FailedAttempt`, `CustomerView`) versus what only the SIMULATOR
knows (`GroundTruth`). Keeping them in separate types is what makes the
evaluation honest -- there is no field the agent could accidentally read to
peek at whether an order was ever going to recover on its own.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Failure taxonomy -- mirrors Razorpay's real error surface
# ---------------------------------------------------------------------------
class FailureClass(str, Enum):
    """Root-cause classes. This is the fixed taxonomy the LLM classifies into;
    it cannot invent a new label."""

    ISSUER_DOWN = "ISSUER_DOWN"                    # bank/gateway transient outage
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"            # timed out mid-authorization
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"      # balance too low, payday-linked
    AUTH_3DS_TIMEOUT = "AUTH_3DS_TIMEOUT"          # customer abandoned OTP/3DS
    CARD_EXPIRED = "CARD_EXPIRED"                  # instrument is dead
    DO_NOT_HONOR = "DO_NOT_HONOR"                  # issuer hard decline, opaque
    UPI_COLLECT_EXPIRED = "UPI_COLLECT_EXPIRED"    # collect request lapsed
    MANDATE_REVOKED = "MANDATE_REVOKED"            # autopay mandate cancelled
    MANDATE_INSUFFICIENT = "MANDATE_INSUFFICIENT"  # mandate live, debit bounced
    RISK_BLOCKED = "RISK_BLOCKED"                  # blocked by risk engine
    CHECKOUT_ABANDONED = "CHECKOUT_ABANDONED"      # never attempted payment
    INVOICE_OVERDUE = "INVOICE_OVERDUE"            # B2B receivable past due
    UNKNOWN = "UNKNOWN"                            # diagnosis fallback


class Intervention(str, Enum):
    """Every action the agent can take. `WAIT` and `WRITE_OFF` are real
    choices, not absences of a choice -- they get logged like anything else."""

    WAIT = "WAIT"
    RETRY_NOW = "RETRY_NOW"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    METHOD_SWITCH_LINK = "METHOD_SWITCH_LINK"   # fresh payment link, nudge to UPI
    UPDATE_INSTRUMENT_LINK = "UPDATE_INSTRUMENT_LINK"
    MANDATE_REPRESENT = "MANDATE_REPRESENT"
    MANDATE_REAUTH_LINK = "MANDATE_REAUTH_LINK"
    NUDGE_EMAIL = "NUDGE_EMAIL"
    NUDGE_SMS = "NUDGE_SMS"
    NUDGE_WHATSAPP = "NUDGE_WHATSAPP"
    VOICE_CALL = "VOICE_CALL"
    HUMAN_COLLECTIONS_CALL = "HUMAN_COLLECTIONS_CALL"
    WRITE_OFF = "WRITE_OFF"


class Channel(str, Enum):
    NONE = "NONE"
    EMAIL = "EMAIL"
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    VOICE = "VOICE"
    GATEWAY = "GATEWAY"   # a charge attempt, not a conversation


# Unit cost of taking each action, in INR. Messaging/telephony rates are
# Indian-market realistic; gateway retries are free unless they succeed.
ACTION_COST_INR: Dict["Intervention", float] = {
    Intervention.WAIT: 0.0,
    Intervention.RETRY_NOW: 0.0,
    Intervention.RETRY_SCHEDULED: 0.0,
    Intervention.METHOD_SWITCH_LINK: 0.05,
    Intervention.UPDATE_INSTRUMENT_LINK: 0.05,
    Intervention.MANDATE_REPRESENT: 0.0,
    Intervention.MANDATE_REAUTH_LINK: 0.05,
    Intervention.NUDGE_EMAIL: 0.05,
    Intervention.NUDGE_SMS: 0.25,
    Intervention.NUDGE_WHATSAPP: 0.80,
    Intervention.VOICE_CALL: 18.00,
    Intervention.HUMAN_COLLECTIONS_CALL: 150.00,
    Intervention.WRITE_OFF: 0.0,
}

ACTION_CHANNEL: Dict["Intervention", "Channel"] = {
    Intervention.WAIT: Channel.NONE,
    Intervention.RETRY_NOW: Channel.GATEWAY,
    Intervention.RETRY_SCHEDULED: Channel.GATEWAY,
    Intervention.MANDATE_REPRESENT: Channel.GATEWAY,
    Intervention.METHOD_SWITCH_LINK: Channel.EMAIL,
    Intervention.UPDATE_INSTRUMENT_LINK: Channel.EMAIL,
    Intervention.MANDATE_REAUTH_LINK: Channel.EMAIL,
    Intervention.NUDGE_EMAIL: Channel.EMAIL,
    Intervention.NUDGE_SMS: Channel.SMS,
    Intervention.NUDGE_WHATSAPP: Channel.WHATSAPP,
    Intervention.VOICE_CALL: Channel.VOICE,
    Intervention.HUMAN_COLLECTIONS_CALL: Channel.NONE,
    Intervention.WRITE_OFF: Channel.NONE,
}

# Actions that move money. These are the ones that need idempotency keys.
MONEY_ACTIONS = {
    Intervention.RETRY_NOW,
    Intervention.RETRY_SCHEDULED,
    Intervention.MANDATE_REPRESENT,
}


class OrderKind(str, Enum):
    ONE_TIME = "ONE_TIME"
    SUBSCRIPTION = "SUBSCRIPTION"
    B2B_INVOICE = "B2B_INVOICE"


class CustomerFlag(str, Enum):
    IN_DISPUTE = "IN_DISPUTE"
    CHARGEBACK_OPEN = "CHARGEBACK_OPEN"
    LEGAL_HOLD = "LEGAL_HOLD"
    DND_REGISTERED = "DND_REGISTERED"
    HIGH_VALUE = "HIGH_VALUE"


# ---------------------------------------------------------------------------
# Observable state -- everything below is fair game for the agent
# ---------------------------------------------------------------------------
class CustomerView(BaseModel):
    customer_id: str
    # Consent + suppression are first-class, not afterthoughts.
    consent_whatsapp: bool = False
    consent_voice: bool = False
    dnd_registered: bool = False
    opted_out: bool = False
    flags: List[CustomerFlag] = Field(default_factory=list)
    prior_orders: int = 0
    prior_successful_orders: int = 0
    locale: str = "en-IN"          # drives Hinglish vs English messaging
    timezone: str = "Asia/Kolkata"


class FailedAttempt(BaseModel):
    """One charge attempt as Razorpay would report it."""

    attempt_id: str
    order_id: str
    attempted_at: datetime
    method: str                     # card | upi | netbanking | wallet | emandate
    issuer: Optional[str] = None    # HDFC, ICICI, SBIN, ...
    gateway: Optional[str] = None   # which PG processed it
    network: Optional[str] = None   # VISA, MASTERCARD, RUPAY
    # Razorpay-shaped error surface. This is the raw text the LLM reads.
    error_code: Optional[str] = None
    error_source: Optional[str] = None
    error_step: Optional[str] = None
    error_reason: Optional[str] = None
    error_description: Optional[str] = None
    was_agent_initiated: bool = False


class Order(BaseModel):
    order_id: str
    customer_id: str
    kind: OrderKind
    amount_inr: float
    currency: str = "INR"
    created_at: datetime
    due_at: Optional[datetime] = None      # for invoices / subscription cycles
    description: str = ""
    attempts: List[FailedAttempt] = Field(default_factory=list)
    # Filled in by the runner, not the generator.
    recovered: bool = False
    recovered_at: Optional[datetime] = None
    recovered_amount_inr: float = 0.0
    is_holdout: bool = False

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def latest_attempt(self) -> Optional["FailedAttempt"]:
        return self.attempts[-1] if self.attempts else None


# ---------------------------------------------------------------------------
# Hidden state -- the simulator's private knowledge
# ---------------------------------------------------------------------------
class GroundTruth(BaseModel):
    """Never exposed to detection, diagnosis, or policy. Used only to (a) drive
    the outcome simulator and (b) compute the recovery ceiling after the fact."""

    order_id: str
    true_class: FailureClass
    # Probability the order recovers on its own with zero intervention. This is
    # the whole reason a holdout is necessary: a naive "we recovered 42%" claim
    # silently takes credit for all of this.
    self_heal_prob: float
    # Per-intervention success probability if that action is taken.
    responsiveness: Dict[str, float] = Field(default_factory=dict)
    # How willing this customer is to complete at all, 0..1.
    intent_strength: float = 0.5
    # True if no sequence of allowed actions could ever recover this order.
    unrecoverable: bool = False


class TrafficEvent(BaseModel):
    """A payment attempt outcome on the merchant's overall traffic.

    Detection needs successes as well as failures: a spike in failure *count*
    is ambiguous (it could just be a traffic spike), whereas a drop in success
    *rate* on one (issuer, method, gateway) cell is a real signal. So the
    corpus carries the successful background traffic too.
    """

    ts: datetime
    method: str
    issuer: Optional[str] = None
    gateway: Optional[str] = None
    success: bool
    amount_inr: float = 0.0
    order_id: Optional[str] = None


class Batch(BaseModel):
    """A generated corpus plus its hidden answer key."""

    batch_id: str
    generated_at: datetime
    seed: int
    orders: List[Order]
    customers: Dict[str, CustomerView]
    ground_truth: Dict[str, GroundTruth]
    traffic: List[TrafficEvent] = Field(default_factory=list)
    # (issuer, method, gateway, start, end) windows we injected, so the
    # detector's findings can be scored against what was actually there.
    injected_outages: List[Dict[str, Any]] = Field(default_factory=list)

    @property
    def total_at_risk_inr(self) -> float:
        return round(sum(o.amount_inr for o in self.orders), 2)
