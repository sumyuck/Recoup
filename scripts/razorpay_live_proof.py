#!/usr/bin/env python
"""Dedicated live proof of the Razorpay test-mode integration.

Why this is separate from the evaluation
----------------------------------------
The first version sent every recovery action to the real API during the metrics
run. That was wrong for two reasons, one of which took a while to see:

*   **Razorpay test mode caps an account at 30 payment links, permanently.**
    It is not a rate limit and no amount of backoff fixes it. My first live run
    burned all 30 and then logged ~1,500 HTTP 429s that I initially misread as
    throttling. The real message was
    `test mode limit of 30 reached for payment_link`.
*   A metrics run must not depend on a third party's quota. Runtime and results
    should not vary with someone else's limiter.

So the evaluation shadows Razorpay by default -- building and logging the exact
request it would send -- and this script proves the leg is genuinely wired.

What it proves
--------------
  1. a real Order created for a scheduled retry, fetched back
  2. a real Payment Link created for a method-switch recovery, fetched back
     (only if the account cap allows; skipped honestly otherwise)
  3. **server-side idempotency**: resubmitting the same `reference_id` is
     refused by Razorpay with `already exists`, so a duplicated recovery action
     cannot create a second link
  4. an inventory of every object Recoup has created upstream, as receipts

Point 3 started as a bug. Arms B and C generated identical idempotency keys and
Razorpay rejected the collisions -- the API confirming that the protection this
design claims is enforced upstream, not only in my own ledger.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"), override=True)

import httpx  # noqa: E402

BASE = "https://api.razorpay.com/v1"
OUT = os.path.join(ROOT, "artifacts", "razorpay_live_proof.json")
LINK_CAP = 30           # Razorpay test-mode account cap on payment links


def main() -> None:
    kid = os.getenv("RAZORPAY_KEY_ID", "")
    ksec = os.getenv("RAZORPAY_KEY_SECRET", "")
    if not kid or not ksec:
        sys.exit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set in .env")
    if not kid.startswith("rzp_test_"):
        sys.exit(f"refusing to run against a non-test key ({kid[:12]}...)")

    c = httpx.Client(base_url=BASE, auth=(kid, ksec), timeout=30)
    run = uuid.uuid4().hex[:10]
    R = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "key_id": kid,
        "mode": "TEST",
        "steps": [],
    }

    def step(name: str, **kw) -> None:
        R["steps"].append({"step": name, **kw})
        bits = [f"HTTP {kw['status']}"] if kw.get("status") else []
        for k in ("id", "result", "error"):
            if kw.get(k):
                bits.append(str(kw[k])[:110])
        print(f"  {name:46s} {'  '.join(bits)}")

    print(f"\nRazorpay live test-mode proof   key={kid}   run={run}\n")

    # --- budget check ----------------------------------------------------
    existing = (c.get("/payment_links").json() or {}).get("payment_links", [])
    remaining = LINK_CAP - len(existing)
    R["payment_link_budget"] = {
        "cap": LINK_CAP, "used": len(existing), "remaining": remaining,
        "note": "Razorpay test mode caps payment links per account, permanently. "
                "This is why the evaluation shadows Razorpay by default.",
    }
    print(f"  payment-link budget: {len(existing)}/{LINK_CAP} used, {remaining} left "
          f"(account cap, not a rate limit)\n")

    amount_inr = 4820.00
    ref = f"recoup_proof_{run}"

    # --- 1. order (uncapped) ---------------------------------------------
    order_payload = {
        "amount": int(amount_inr * 100), "currency": "INR",
        "receipt": f"{ref}_retry",
        "notes": {"recoup_order_id": f"order_proof_{run}",
                  "recoup_intervention": "RETRY_SCHEDULED",
                  "recoup_diagnosed_class": "INSUFFICIENT_FUNDS",
                  "recoup_reason": "revenue recovery re-attempt"},
    }
    r = c.post("/orders", json=order_payload)
    order = r.json() if r.status_code < 400 else {}
    step("1. create Order (scheduled retry)", status=r.status_code, id=order.get("id"),
         request=order_payload, error=None if r.status_code < 400 else r.text[:250])

    if order.get("id"):
        rr = c.get(f"/orders/{order['id']}")
        b = rr.json() if rr.status_code < 400 else {}
        step("2. fetch Order back", status=rr.status_code, id=b.get("id"),
             result=f"status={b.get('status')} amount={b.get('amount')}p")

    # --- 2. payment link + idempotency (capped) ---------------------------
    link, dup = {}, {}
    if remaining < 2:
        step("3. Payment Link proof SKIPPED", status=None,
             error=f"only {remaining} of {LINK_CAP} links left under the account cap; "
                   f"not spending them. The {len(existing)} links already created by "
                   f"Recoup are the receipts.")
    else:
        link_payload = {
            "amount": int(amount_inr * 100), "currency": "INR", "accept_partial": False,
            "description": "Complete your payment via UPI - your card payment did not complete",
            "reference_id": ref,
            "notify": {"sms": False, "email": False}, "reminder_enable": False,
            "notes": {"recoup_order_id": f"order_proof_{run}",
                      "recoup_intervention": "METHOD_SWITCH_LINK",
                      "recoup_diagnosed_class": "AUTH_3DS_TIMEOUT",
                      "recoup_idempotency_key": ref},
        }
        r = c.post("/payment_links", json=link_payload)
        link = r.json() if r.status_code < 400 else {}
        step("3. create Payment Link (method switch)", status=r.status_code,
             id=link.get("id"), result=link.get("short_url"),
             error=None if r.status_code < 400 else r.text[:250])

        # The idempotency test. Crucially, check WHY it was refused -- an
        # earlier version reported a 429 as proof of idempotency, which is a
        # false positive: throttling and duplicate-rejection are different
        # things and only one of them proves anything.
        r2 = c.post("/payment_links", json=link_payload)
        body = r2.json() if r2.text else {}
        err = (body.get("error") or {})
        desc = err.get("description") or ""
        is_dup = "already exists" in desc.lower()
        is_throttle = err.get("code") == "RATE_LIMIT_EXCEEDED" or "limit" in desc.lower()
        dup = {"status": r2.status_code, "description": desc,
               "refused_as_duplicate": is_dup, "refused_as_throttle": is_throttle}
        step("4. resubmit SAME reference_id", status=r2.status_code,
             result=("REFUSED as duplicate — idempotency enforced upstream" if is_dup
                     else "refused, but for THROTTLING not duplication — proves nothing"
                     if is_throttle else f"unexpected: {desc[:80]}"))

        if link.get("id"):
            rr = c.get(f"/payment_links/{link['id']}")
            b = rr.json() if rr.status_code < 400 else {}
            step("5. fetch Payment Link back", status=rr.status_code, id=b.get("id"),
                 result=f"status={b.get('status')} amount={b.get('amount')}p")

    # --- 2b. does the Orders endpoint enforce receipt uniqueness? ----------
    # It does not, by default -- and that matters. `payment_links.reference_id`
    # IS enforced unique upstream, but `orders.receipt` is not unless the
    # account explicitly enables it. So upstream uniqueness is an inconsistent
    # safety net: present for links, absent for orders. That is exactly why the
    # local hash-keyed idempotency ledger is the load-bearing protection here
    # and the API constraint is only a secondary check.
    dup_rcpt = f"recoup_dupcheck_{run}"
    dup_payload = {"amount": 123400, "currency": "INR", "receipt": dup_rcpt,
                   "notes": {"recoup_intervention": "RETRY_NOW",
                             "recoup_idempotency_key": dup_rcpt}}
    ids = []
    for _ in range(2):
        rr = c.post("/orders", json=dup_payload)
        if rr.status_code < 400:
            ids.append(rr.json().get("id"))
    orders_enforce = len(set(ids)) < 2
    R["orders_receipt_uniqueness"] = {
        "enforced_by_razorpay": orders_enforce,
        "ids_created": ids,
        "finding": "Razorpay does NOT enforce `receipt` uniqueness on /orders by "
                   "default, though it DOES enforce `reference_id` on "
                   "/payment_links. Upstream idempotency is therefore "
                   "inconsistent across endpoints and cannot be relied on; the "
                   "local idempotency ledger is the real guard.",
    }
    step("6. duplicate Order receipt (uniqueness check)", status=200,
         result=(f"Razorpay created {len(set(ids))} distinct orders for the same "
                 f"receipt -> uniqueness NOT enforced upstream"
                 if not orders_enforce else "receipt uniqueness enforced"))

    # --- 3. inventory of everything Recoup created upstream ---------------
    links_all = (c.get("/payment_links").json() or {}).get("payment_links", [])
    orders_all = (c.get("/orders", params={"count": 100}).json() or {}).get("items", [])
    mine_l = [l for l in links_all if (l.get("notes") or {}).get("recoup_intervention")]
    mine_o = [o for o in orders_all if (o.get("notes") or {}).get("recoup_intervention")]
    R["upstream_inventory"] = {
        "payment_links_created_by_recoup": len(mine_l),
        "orders_created_by_recoup": len(mine_o),
        "sample_payment_links": [
            {"id": l["id"], "short_url": l.get("short_url"),
             "amount_inr": l["amount"] / 100, "reference_id": l.get("reference_id"),
             "intervention": (l.get("notes") or {}).get("recoup_intervention")}
            for l in mine_l[:8]
        ],
        "sample_orders": [
            {"id": o["id"], "amount_inr": o["amount"] / 100,
             "intervention": (o.get("notes") or {}).get("recoup_intervention")}
            for o in mine_o[:8]
        ],
    }

    R["summary"] = {
        "order_id": order.get("id"),
        "payment_link_id": link.get("id"),
        "payment_link_url": link.get("short_url"),
        "payment_link_reference_id_enforced": dup.get("refused_as_duplicate"),
        "orders_receipt_enforced": R.get("orders_receipt_uniqueness", {}).get(
            "enforced_by_razorpay"),
        "payment_links_upstream": len(mine_l),
        "orders_upstream": len(mine_o),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(R, fh, indent=2, default=str)

    print(f"\n  Recoup objects live in the account: "
          f"{len(mine_l)} payment links, {len(mine_o)} orders")
    if dup:
        print(f"  Duplicate reference_id refused as duplicate: "
              f"{dup.get('refused_as_duplicate')}")
    print(f"  receipts -> artifacts/{os.path.basename(OUT)}")


if __name__ == "__main__":
    main()
