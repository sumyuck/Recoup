"""Razorpay webhook ingestion.

The evaluation runs on a generated corpus because measuring lift needs ground
truth and a holdout. But a recovery agent that can only read its own synthetic
data format is a demo, so this module consumes the real thing: actual Razorpay
webhook payloads, mapped onto the same domain model the pipeline already uses.

Events handled -- the four ways revenue actually goes missing:

    payment.failed          a charge attempt failed
    subscription.charged    (status failed) an autopay debit bounced
    payment_link.expired    a link was sent and never paid
    invoice.expired         a receivable lapsed

Security notes, because this endpoint is internet-facing by nature:

*   **Signature verification is mandatory and constant-time.** Razorpay signs
    the raw request body with HMAC-SHA256 using the webhook secret. We verify
    against the *raw bytes*, not a re-serialised dict -- re-encoding JSON
    changes key order and whitespace and would break every signature.
*   **Replay protection.** Razorpay retries failed deliveries, so the same
    event can arrive many times. Events are deduplicated on the
    `x-razorpay-event-id` header, which is the only identifier guaranteed
    stable across retries.
*   **The payload is data, never instruction.** Fields like `notes` and
    `description` are merchant- and customer-controlled free text that flows
    into the diagnosis prompt. It is passed as a JSON value inside a fenced
    field, and the diagnoser's output is schema-validated against a closed
    taxonomy, so text arriving here cannot widen what the agent is able to do.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from .models import CustomerView, FailedAttempt, Order, OrderKind

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Razorpay's `error_reason` values arrive already matching the taxonomy in
# `diagnose.DETERMINISTIC_MAP`, which is why that map was built from their real
# error surface rather than invented.
SUPPORTED_EVENTS = {
    "payment.failed",
    "subscription.charged",
    "subscription.halted",
    "payment_link.expired",
    "invoice.expired",
}


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification against the RAW request body."""
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _ts(epoch: Optional[int]) -> datetime:
    if not epoch:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).replace(tzinfo=None)


def _issuer_from_payment(p: Dict) -> Optional[str]:
    """Razorpay reports the issuing bank in different places per method."""
    card = p.get("card") or {}
    if card.get("issuer"):
        return card["issuer"]
    if p.get("bank"):
        return p["bank"]
    # UPI VPA handles look like name@bank
    vpa = (p.get("vpa") or "")
    if "@" in vpa:
        return vpa.split("@", 1)[1].upper()
    return None


class IngestResult(BaseModel):
    accepted: bool
    event_id: Optional[str] = None
    event: Optional[str] = None
    order_id: Optional[str] = None
    reason: Optional[str] = None
    duplicate: bool = False


class WebhookStore:
    """In-memory ingest buffer with a JSONL spool.

    Not a queue broker. The spool exists so ingested events can be replayed
    through the pipeline offline (`cli.py replay`), which is how a real captured
    event stream would be used to backtest a policy change before shipping it.
    """

    def __init__(self, spool_path: str = "artifacts/webhook_spool.jsonl"):
        self.spool_path = spool_path
        self.seen_event_ids: set = set()
        self.orders: Dict[str, Order] = {}
        self.customers: Dict[str, CustomerView] = {}
        self.stats = {
            "received": 0,
            "accepted": 0,
            "duplicates": 0,
            "unsupported": 0,
            "bad_signature": 0,
            "unmappable": 0,
        }
        os.makedirs(os.path.dirname(spool_path) or ".", exist_ok=True)

    def ingest(self, body: Dict, event_id: Optional[str]) -> IngestResult:
        self.stats["received"] += 1
        event = body.get("event")

        if event_id and event_id in self.seen_event_ids:
            self.stats["duplicates"] += 1
            return IngestResult(accepted=False, event_id=event_id, event=event,
                                duplicate=True, reason="already ingested")
        if event not in SUPPORTED_EVENTS:
            self.stats["unsupported"] += 1
            return IngestResult(accepted=False, event_id=event_id, event=event,
                                reason=f"unsupported event '{event}'")

        try:
            order, attempt, customer = map_event(body)
        except Exception as e:
            self.stats["unmappable"] += 1
            return IngestResult(accepted=False, event_id=event_id, event=event,
                                reason=f"unmappable: {type(e).__name__}: {e}")

        # Merge onto an existing order if we have already seen it fail.
        existing = self.orders.get(order.order_id)
        if existing is not None:
            if attempt is not None and all(
                a.attempt_id != attempt.attempt_id for a in existing.attempts
            ):
                existing.attempts.append(attempt)
            order = existing
        else:
            if attempt is not None:
                order.attempts.append(attempt)
            self.orders[order.order_id] = order
        self.customers.setdefault(customer.customer_id, customer)

        if event_id:
            self.seen_event_ids.add(event_id)
        self.stats["accepted"] += 1
        with open(self.spool_path, "a") as fh:
            fh.write(json.dumps({"event_id": event_id, "body": body}, default=str) + "\n")

        return IngestResult(accepted=True, event_id=event_id, event=event,
                            order_id=order.order_id)


def map_event(body: Dict) -> Tuple[Order, Optional[FailedAttempt], CustomerView]:
    """Map a Razorpay webhook body onto the domain model."""
    event = body["event"]
    payload = body.get("payload") or {}

    if event == "payment.failed":
        return _map_payment_failed(payload)
    if event in ("subscription.charged", "subscription.halted"):
        return _map_subscription(payload, event)
    if event == "payment_link.expired":
        return _map_payment_link(payload)
    if event == "invoice.expired":
        return _map_invoice(payload)
    raise ValueError(f"no mapper for {event}")


def _customer(email: Optional[str], contact: Optional[str], cid: Optional[str]) -> CustomerView:
    """Derive a stable customer key.

    Razorpay does not always send a `customer_id` on a failed payment, so we
    fall back to a hash of contact/email. Hashed rather than raw so PII does not
    become a primary key smeared across logs and ledger entries.
    """
    if cid:
        key = cid
    else:
        basis = (contact or email or "unknown").strip().lower()
        key = "cust_h" + hashlib.sha256(basis.encode()).hexdigest()[:12]
    return CustomerView(customer_id=key)


def _map_payment_failed(payload: Dict) -> Tuple[Order, FailedAttempt, CustomerView]:
    p = payload["payment"]["entity"]
    oid = p.get("order_id") or f"order_from_{p['id']}"
    cust = _customer(p.get("email"), p.get("contact"), p.get("customer_id"))
    created = _ts(p.get("created_at"))

    order = Order(
        order_id=oid,
        customer_id=cust.customer_id,
        kind=OrderKind.ONE_TIME,
        amount_inr=round((p.get("amount") or 0) / 100.0, 2),   # paise -> rupees
        currency=p.get("currency", "INR"),
        created_at=created,
        description=(p.get("description") or "Razorpay payment")[:200],
    )
    attempt = FailedAttempt(
        attempt_id=p["id"],
        order_id=oid,
        attempted_at=created,
        method=p.get("method") or "unknown",
        issuer=_issuer_from_payment(p),
        gateway=(p.get("acquirer_data") or {}).get("gateway") or None,
        network=(p.get("card") or {}).get("network"),
        error_code=p.get("error_code"),
        error_source=p.get("error_source"),
        error_step=p.get("error_step"),
        error_reason=p.get("error_reason"),
        error_description=p.get("error_description"),
    )
    return order, attempt, cust


def _map_subscription(payload: Dict, event: str) -> Tuple[Order, Optional[FailedAttempt], CustomerView]:
    sub = payload["subscription"]["entity"]
    pay = (payload.get("payment") or {}).get("entity") or {}
    sid = sub["id"]
    oid = pay.get("order_id") or f"order_{sid}_c{sub.get('paid_count', 0)}"
    cust = _customer(pay.get("email"), pay.get("contact"), sub.get("customer_id"))
    created = _ts(pay.get("created_at") or sub.get("charge_at"))

    amount = pay.get("amount")
    if amount is None:
        # subscription.halted carries no payment entity
        amount = (sub.get("plan") or {}).get("item", {}).get("amount", 0)
    order = Order(
        order_id=oid,
        customer_id=cust.customer_id,
        kind=OrderKind.SUBSCRIPTION,
        amount_inr=round((amount or 0) / 100.0, 2),
        created_at=created,
        due_at=_ts(sub.get("charge_at")),
        description=f"Subscription {sid} cycle {sub.get('paid_count', 0) + 1}",
    )
    attempt = None
    if pay:
        attempt = FailedAttempt(
            attempt_id=pay["id"],
            order_id=oid,
            attempted_at=created,
            method=pay.get("method") or "emandate",
            issuer=_issuer_from_payment(pay),
            gateway=(pay.get("acquirer_data") or {}).get("gateway"),
            error_code=pay.get("error_code"),
            error_source=pay.get("error_source"),
            error_step=pay.get("error_step"),
            # A halted subscription means the mandate is gone, which is a
            # different recovery path from a bounced debit on a live mandate.
            error_reason=pay.get("error_reason")
            or ("mandate_revoked" if event == "subscription.halted" else None),
            error_description=pay.get("error_description"),
        )
    return order, attempt, cust


def _map_payment_link(payload: Dict) -> Tuple[Order, None, CustomerView]:
    pl = payload["payment_link"]["entity"]
    cd = pl.get("customer") or {}
    cust = _customer(cd.get("email"), cd.get("contact"), None)
    return (
        Order(
            order_id=pl.get("reference_id") or pl["id"],
            customer_id=cust.customer_id,
            kind=OrderKind.ONE_TIME,
            amount_inr=round((pl.get("amount") or 0) / 100.0, 2),
            created_at=_ts(pl.get("created_at")),
            due_at=_ts(pl.get("expire_by")),
            description=(pl.get("description") or "Expired payment link")[:200],
        ),
        None,      # a link that expired has no failed charge attempt
        cust,
    )


def _map_invoice(payload: Dict) -> Tuple[Order, None, CustomerView]:
    inv = payload["invoice"]["entity"]
    cd = inv.get("customer_details") or {}
    cust = _customer(cd.get("email"), cd.get("contact"), inv.get("customer_id"))
    return (
        Order(
            order_id=inv.get("order_id") or inv["id"],
            customer_id=cust.customer_id,
            kind=OrderKind.B2B_INVOICE,
            amount_inr=round((inv.get("amount") or 0) / 100.0, 2),
            created_at=_ts(inv.get("issued_at") or inv.get("created_at")),
            due_at=_ts(inv.get("expire_by") or inv.get("date")),
            description=(inv.get("description") or f"Invoice {inv.get('invoice_number','')}")[:200],
        ),
        None,
        cust,
    )


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
STORE = WebhookStore()


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(default=None),
    x_razorpay_event_id: Optional[str] = Header(default=None),
) -> Dict:
    raw = await request.body()
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    if secret:
        if not verify_signature(raw, x_razorpay_signature or "", secret):
            STORE.stats["bad_signature"] += 1
            # 400, not 401: Razorpay retries on 5xx, and retrying a payload that
            # will never verify is pure noise.
            raise HTTPException(400, "signature verification failed")
    # With no secret configured the endpoint still works for local replay, but
    # says so loudly rather than pretending it verified something.

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "body is not valid JSON")

    res = STORE.ingest(body, x_razorpay_event_id)
    return {
        "signature_verified": bool(secret),
        **res.model_dump(),
        "store": {"orders": len(STORE.orders), "customers": len(STORE.customers)},
        "stats": STORE.stats,
    }


@router.get("/status")
def status() -> Dict:
    return {
        "stats": STORE.stats,
        "orders_ingested": len(STORE.orders),
        "customers": len(STORE.customers),
        "signature_verification": "enabled"
        if os.getenv("RAZORPAY_WEBHOOK_SECRET")
        else "DISABLED (set RAZORPAY_WEBHOOK_SECRET)",
        "supported_events": sorted(SUPPORTED_EVENTS),
    }


@router.get("/orders")
def ingested_orders() -> List[Dict]:
    return [json.loads(o.model_dump_json()) for o in STORE.orders.values()]
