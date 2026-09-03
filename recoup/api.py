"""FastAPI app serving the report and the per-order audit trail.

Read-only by design. The dashboard shows what a run decided and why; it cannot
trigger an action. A recovery console that can also *launch* recovery is a
second, much more dangerous product, and conflating them would put an
unauthenticated button in front of real money movement.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

ART = "artifacts"
HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Recoup", docs_url="/api/docs")


def _report() -> Dict:
    p = os.path.join(ART, "report.json")
    if not os.path.exists(p):
        raise HTTPException(404, "no report yet -- run: python cli.py eval")
    with open(p) as fh:
        return json.load(fh)


def _ledger(arm: str = "C_AGENT") -> List[Dict]:
    p = os.path.join(ART, f"ledger_{arm}.jsonl")
    if not os.path.exists(p):
        raise HTTPException(404, f"no ledger for {arm}")
    out = []
    with open(p) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    with open(os.path.join(HERE, "dashboard", "index.html")) as fh:
        return fh.read()


@app.get("/api/report")
def report() -> JSONResponse:
    return JSONResponse(_report())


@app.get("/api/incidents")
def incidents(arm: str = "C_AGENT") -> JSONResponse:
    sigs = [e["payload"] for e in _ledger(arm) if e["event"] == "SIGNAL_RAISED"]
    return JSONResponse(sigs)


@app.get("/api/orders")
def orders(arm: str = "C_AGENT", limit: int = 400) -> JSONResponse:
    """One row per order, with enough state for the table and a drill-down."""
    led = _ledger(arm)
    rows: Dict[str, Dict] = {}
    for e in led:
        oid = e.get("order_id")
        if not oid:
            continue
        r = rows.setdefault(
            oid,
            {
                "order_id": oid,
                "customer_id": e.get("customer_id"),
                "arm": e.get("arm"),
                "diagnosed_class": None,
                "confidence": None,
                "tier": None,
                "actions": [],
                "denials": [],
                "recovered": False,
                "recovered_via": None,
                "amount_inr": None,
                "stop_reason": None,
                "spend_inr": 0.0,
                "events": 0,
                "escalated": False,
                # Holdout orders are deliberately untouched. Without this flag
                # they render as "open" in the table, which reads as the agent
                # having ignored them -- the opposite of the truth.
                "is_holdout": False,
            },
        )
        if e.get("arm") == "HOLDOUT":
            r["is_holdout"] = True
        r["events"] += 1
        r["spend_inr"] += e.get("cost_inr", 0.0) or 0.0
        p = e["payload"]
        ev = e["event"]
        if ev == "DIAGNOSIS":
            r["diagnosed_class"] = p.get("failure_class")
            r["confidence"] = p.get("confidence")
            r["tier"] = p.get("tier")
        elif ev == "ACTION_EXECUTED" and p.get("intervention") != "VOICE_CALL_SCRIPT":
            r["actions"].append(p.get("intervention"))
        elif ev == "POLICY_VERDICT" and not p.get("allowed"):
            if p.get("denial_rule"):
                r["denials"].append(p["denial_rule"])
        elif ev == "OUTCOME_OBSERVED":
            if p.get("recovered"):
                r["recovered"] = True
                r["recovered_via"] = p.get("via")
            if p.get("amount_inr") is not None:
                r["amount_inr"] = p["amount_inr"]
        elif ev == "SEQUENCE_STOPPED":
            r["stop_reason"] = p.get("reason")
        elif ev == "HUMAN_ESCALATED":
            r["escalated"] = True
    out = sorted(rows.values(), key=lambda r: -(r["amount_inr"] or 0))
    return JSONResponse(out[:limit])


@app.get("/api/trace/{order_id}")
def trace(order_id: str, arm: str = "C_AGENT") -> JSONResponse:
    """The full decision trail for one order -- the audit view."""
    led = [e for e in _ledger(arm) if e.get("order_id") == order_id]
    if not led:
        raise HTTPException(404, f"no trail for {order_id}")
    return JSONResponse(led)


@app.get("/api/ledger/verify")
def verify(arm: str = "C_AGENT") -> JSONResponse:
    from .ledger import verify_file

    return JSONResponse(verify_file(os.path.join(ART, f"ledger_{arm}.jsonl")))


@app.get("/api/voice")
def voice(arm: str = "C_AGENT") -> JSONResponse:
    p = os.path.join(ART, f"voice_{arm}.jsonl")
    if not os.path.exists(p):
        return JSONResponse([])
    out = []
    with open(p) as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return JSONResponse(out)


@app.get("/api/policy")
def policy() -> JSONResponse:
    """Serve the policy file itself, so the guardrails are inspectable from the
    UI rather than described in it."""
    with open("policy.yaml") as fh:
        return JSONResponse({"policy_yaml": fh.read()})
