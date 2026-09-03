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
from time import sleep, time

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
        run_id: str = "run",
        live_sample: Optional[int] = None,
        min_interval_s: float = 0.35,
    ):
        # `reference_id` must be globally unique in the Razorpay account, not
        # just unique within one run. The local idempotency key is
        # sha256(order_id, intervention, attempt_idx) -- deliberately stable, so
        # a resubmission inside a run is caught -- but that means two arms of
        # the same experiment, or a re-run of the same seed, generate the
        # IDENTICAL key and Razorpay correctly rejects the second one with
        # "payment link with given reference_id already exists".
        #
        # Seeing that error was actually reassuring: it means the server-side
        # idempotency this design claims is genuinely enforced upstream, not
        # only in my own ledger. But the reference needs a run namespace.
        self.run_id = run_id
        # Razorpay test mode is aggressively rate-limited. A 500-order batch
        # generates a few hundred writes across two arms and produced ~3,300
        # HTTP 429s. Rather than pretend otherwise, send a documented sample
        # live and shadow the remainder, so the measurement is never hostage to
        # someone else's throttle.
        self.live_sample = live_sample
        # Per-endpoint pacing, because the limits are not uniform. Measured on
        # a real test account: /orders sustains a batch comfortably, while
        # /payment_links throttles hard enough that a few hundred writes
        # produced ~1,500 HTTP 429s. One global interval either crawls for
        # everything or gets throttled on links.
        self.min_interval_s = min_interval_s
        self.path_interval_s = {"/payment_links": 3.0, "/orders": 0.35}
        self._last_call_at: Dict[str, float] = {}
        self._live_calls = 0
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
        # Fresh log per run. These files were appending across every run, so a
        # later inspection mixed shadow calls from one run with real ones from
        # another.
        if log_path:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            open(log_path, "w").close()
        self.calls: list = []
        self.stats = {
            "sent": 0,            # successful upstream writes
            "shadow": 0,
            "errors": 0,
            "rate_limited": 0,    # 429 responses (retries, not distinct actions)
            "http_attempts": 0,   # every request issued, retries included
            "sampled_out": 0,     # shadowed because the live sample was spent
            "throttled_to_shadow": 0,   # 429 after all retries -> shadowed
        }

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
            "reference_id": f"recoup_{self.run_id}_{idem[:16]}",
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
            "receipt": f"recoup_{self.run_id}_{idem[:16]}",
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

        # Live budget spent -> shadow the rest, and say so in the record.
        if self.live and self.live_sample is not None and self._live_calls >= self.live_sample:
            self.stats["sampled_out"] += 1
            self.stats["shadow"] += 1
            record["mode"] = "shadow"
            record["reason"] = f"live sample of {self.live_sample} already spent"
            record["would_send_to"] = BASE + path
            self._record(record)
            return f"shadow_{idem[:16]}"

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
            #
            # 429s are retried with backoff rather than dropped: a batch of a
            # few hundred recovery actions will hit the rate limit, and losing
            # the Razorpay reference for an action we already took locally
            # would leave the ledger claiming an API call that has no
            # counterpart upstream.
            # Space out requests. Cheaper than discovering the limit by 429.
            interval = self.path_interval_s.get(path, self.min_interval_s)
            gap = time() - self._last_call_at.get(path, 0.0)
            if gap < interval:
                sleep(interval - gap)

            resp = None
            for attempt in range(4):
                resp = self._client.request(method, path, json=payload)
                self.stats["http_attempts"] += 1
                self._last_call_at[path] = time()
                if resp.status_code != 429:
                    break
                self.stats["rate_limited"] += 1
                sleep(1.5 * (2 ** attempt))     # 1.5, 3, 6, 12s
            self._live_calls += 1

            # A 429 that survives every retry is somebody else's throttle, not a
            # broken integration. Recording it as an error would misreport the
            # health of the Razorpay leg, so it degrades to shadow and is
            # counted separately.
            if resp.status_code == 429:
                self.stats["throttled_to_shadow"] += 1
                self.stats["shadow"] += 1
                record["mode"] = "shadow"
                record["reason"] = "HTTP 429 after retries; degraded to shadow"
                self._record(record)
                return f"shadow_{idem[:16]}"

            if resp.status_code >= 400:
                self.stats["errors"] += 1
                record["mode"] = "error"
                record["status"] = resp.status_code
                record["body"] = resp.text[:400]
                self._record(record)
                return None
            self.stats["sent"] += 1
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
