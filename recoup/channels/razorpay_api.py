"""Razorpay test-mode client.

Scope is deliberately narrow: the actions the recovery agent actually needs.

  METHOD_SWITCH_LINK      -> Payment Link, nudging the customer to UPI
  UPDATE_INSTRUMENT_LINK  -> Payment Link for a fresh card
  MANDATE_REAUTH_LINK     -> Payment Link to re-authorize a lapsed mandate
  RETRY_NOW / SCHEDULED   -> Order created for the re-attempt

Three safety properties, in order of how much they matter:

1.  **Test mode is enforced, not assumed.** The client refuses to start unless
    the key id begins with `rzp_test_`. A live key in a recovery agent that
    sends payment links to customers is not a bug you get to fix afterwards.

2.  **The agent's idempotency key is passed through** as a `notes` field and as
    a natural reference, so a duplicate submission is traceable on Razorpay's
    side too, not just in our own ledger.

3.  **Shadow mode by default.** With `RECOUP_LIVE_RAZORPAY=0` the client builds
    and logs the exact request it would send without sending it. The evaluation
    never depends on network availability, and a rate limit cannot corrupt a
    measured result.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional

import httpx

from ..models import Intervention, Order

BASE = "https://api.razorpay.com/v1"

LINK_ACTIONS = {
    Intervention.METHOD_SWITCH_LINK: (
        "Complete your payment via UPI",
        "Your card payment did not go through. Pay instantly with UPI instead.",
    ),
    Intervention.UPDATE_INSTRUMENT_LINK: (
        "Update your payment method",
        "Your saved card has expired. Use a different method to complete this payment.",
    ),
    Intervention.MANDATE_REAUTH_LINK: (
        "Re-authorize your autopay mandate",
        "Your autopay mandate is no longer active. Re-authorize to continue your plan.",
    ),
}

ORDER_ACTIONS = {
    Intervention.RETRY_NOW,
    Intervention.RETRY_SCHEDULED,
    Intervention.MANDATE_REPRESENT,
}


class RazorpayTestClient:
    def __init__(
        self,
        key_id: Optional[str] = None,
        key_secret: Optional[str] = None,
        live: Optional[bool] = None,
        timeout: float = 10.0,
        log_path: Optional[str] = None,
    ):
        self.key_id = key_id or os.getenv("RAZORPAY_KEY_ID", "")
        self.key_secret = key_secret or os.getenv("RAZORPAY_KEY_SECRET", "")
        if live is None:
            live = os.getenv("RECOUP_LIVE_RAZORPAY", "0") == "1"
        self.live = bool(live)

        if self.live:
            if not self.key_id or not self.key_secret:
                raise RuntimeError("RECOUP_LIVE_RAZORPAY=1 but Razorpay keys are not set")
            # Hard guard. Not a warning, not a config flag -- a refusal.
            if not self.key_id.startswith("rzp_test_"):
                raise RuntimeError(
                    f"refusing to run against a non-test key ({self.key_id[:12]}...). "
                    "Recoup sends payment links and creates orders; it must never "
                    "touch a live merchant account."
                )
            # The placeholder from .env.example passes the rzp_test_ check, and
            # left in place it produces a few hundred 401s that look like a
            # broken integration rather than an unset variable. Fail fast and
            # say which it is.
            if "xxxx" in self.key_id.lower() or "xxxx" in self.key_secret.lower():
                raise RuntimeError(
                    "RAZORPAY_KEY_ID/SECRET still hold the .env.example placeholder "
                    "values. Either put real rzp_test_ keys in .env, or set "
                    "RECOUP_LIVE_RAZORPAY=0 to run in shadow mode."
                )
            self._client = httpx.Client(
                base_url=BASE, auth=(self.key_id, self.key_secret), timeout=timeout
            )
        else:
            self._client = None

        self.log_path = log_path
        self.calls: list = []
        self.stats = {"sent": 0, "shadow": 0, "errors": 0}

    # -- the single entry point the executor uses ---------------------------
    def perform(self, order: Order, intervention: Intervention, idem_key: str) -> Optional[str]:
        """Perform the API-visible part of an action. Returns a reference id."""
        if intervention in LINK_ACTIONS:
            return self._payment_link(order, intervention, idem_key)
        if intervention in ORDER_ACTIONS:
            return self._order(order, intervention, idem_key)
        return None      # comms-only actions have no Razorpay footprint

    # -- request builders ---------------------------------------------------
    def _payment_link(self, order: Order, iv: Intervention, idem: str) -> Optional[str]:
        title, desc = LINK_ACTIONS[iv]
        payload: Dict = {
            "amount": int(round(order.amount_inr * 100)),      # paise
            "currency": "INR",
            "accept_partial": False,
            "description": f"{title} - {desc}",
            "reference_id": f"recoup_{idem[:20]}",
            "notify": {"sms": False, "email": False},   # Recoup owns comms, not RZP
            "reminder_enable": False,
            "notes": {
                "recoup_order_id": order.order_id,
                "recoup_intervention": iv.value,
                "recoup_idempotency_key": idem,
            },
        }
        return self._send("POST", "/payment_links", payload, idem)

    def _order(self, order: Order, iv: Intervention, idem: str) -> Optional[str]:
        payload: Dict = {
            "amount": int(round(order.amount_inr * 100)),
            "currency": "INR",
            "receipt": f"recoup_{idem[:20]}",
            "notes": {
                "recoup_order_id": order.order_id,
                "recoup_intervention": iv.value,
                "recoup_idempotency_key": idem,
                "recoup_reason": "revenue recovery re-attempt",
            },
        }
        return self._send("POST", "/orders", payload, idem)

    def _send(self, method: str, path: str, payload: Dict, idem: str) -> Optional[str]:
        record = {"method": method, "path": path, "payload": payload, "idem_key": idem}

        if not self.live:
            self.stats["shadow"] += 1
            record["mode"] = "shadow"
            record["would_send_to"] = BASE + path
            self._record(record)
            # A deterministic pseudo-reference, so shadow runs are reproducible.
            return f"shadow_{idem[:16]}"

        try:
            # `reference_id` / `receipt` carry our idempotency key, so a
            # resubmission is detectable server-side as well as locally.
            resp = self._client.request(method, path, json=payload)
            self.stats["sent"] += 1
            if resp.status_code >= 400:
                self.stats["errors"] += 1
                record["mode"] = "error"
                record["status"] = resp.status_code
                record["body"] = resp.text[:400]
                self._record(record)
                return None
            data = resp.json()
            record["mode"] = "sent"
            record["status"] = resp.status_code
            record["response_id"] = data.get("id")
            record["short_url"] = data.get("short_url")
            self._record(record)
            return data.get("id")
        except Exception as e:
            self.stats["errors"] += 1
            record["mode"] = "exception"
            record["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            self._record(record)
            return None

    def _record(self, record: Dict) -> None:
        self.calls.append(record)
        if self.log_path:
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
