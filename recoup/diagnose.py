"""Diagnosis layer -- the one place an LLM genuinely earns its keep.

Architecture: **tiered**, not LLM-first.

    tier 1  deterministic map   error_reason -> FailureClass
    tier 2  LLM                 only for genuinely ambiguous cases
    tier 3  fallback            schema violation or API failure -> tier 1 answer

Most payment failures carry an unambiguous `error_reason`. `invalid_card_expiry`
means the card is dead; there is no interpretation to do, and paying a model to
restate a lookup table would be waste dressed up as AI. So tier 1 handles the
bulk at zero marginal cost and zero latency.

The LLM is reserved for cases where the structured fields genuinely conflict or
under-determine the answer:

  * `insufficient_funds` on an attempt sitting inside a detected gateway outage
    -- is this really the customer's balance, or the bank misreporting during an
    incident? The right recovery action differs completely.
  * an `error_reason` we have no mapping for
  * B2B invoices, where the class is a judgement about the counterparty rather
    than a code lookup
  * free-text `error_description` that contradicts the structured reason

This split is also the honest answer to "does the AI actually add anything?" --
we measure accuracy on both tiers separately, so the model has to earn the
share of traffic it gets.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from .detect import RiskFacts
from .models import FailureClass

# ---------------------------------------------------------------------------
# Tier 1: the unambiguous lookup
# ---------------------------------------------------------------------------
DETERMINISTIC_MAP: Dict[str, FailureClass] = {
    "invalid_card_expiry": FailureClass.CARD_EXPIRED,
    "card_expired": FailureClass.CARD_EXPIRED,
    "payment_authentication_failed": FailureClass.AUTH_3DS_TIMEOUT,
    "upi_collect_request_expired": FailureClass.UPI_COLLECT_EXPIRED,
    "mandate_revoked": FailureClass.MANDATE_REVOKED,
    "payment_blocked_risk": FailureClass.RISK_BLOCKED,
    "gateway_timeout": FailureClass.GATEWAY_TIMEOUT,
    "payment_declined_by_bank": FailureClass.DO_NOT_HONOR,
    "checkout_abandoned": FailureClass.CHECKOUT_ABANDONED,
    "invoice_overdue": FailureClass.INVOICE_OVERDUE,
}

# These need context, not a lookup:
#   insufficient_funds      -> one-time vs mandate debit are different problems
#   gateway_technical_error -> could be a real outage or an isolated blip
AMBIGUOUS_REASONS = {"insufficient_funds", "gateway_technical_error"}

ALLOWED_CLASSES = [c.value for c in FailureClass]


class Diagnosis(BaseModel):
    """Schema-validated diagnosis. The LLM cannot return anything else."""

    order_id: str
    failure_class: FailureClass
    confidence: float = Field(ge=0.0, le=1.0)
    # Evidence must cite the observable fields it relied on, so a human can
    # check the reasoning instead of taking the label on faith.
    evidence: List[str] = Field(default_factory=list)
    reasoning: str = ""
    tier: str = "deterministic"          # deterministic | llm | fallback
    cost_inr: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    prompt_hash: Optional[str] = None
    model_version: Optional[str] = None
    # Set when the LLM returned something unusable and we fell back.
    fallback_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# LLM plumbing
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a payments failure analyst for an Indian payment gateway.

You classify a single failed payment into exactly one root-cause class, then
justify it from the evidence you were given.

Rules:
- Choose exactly one class from the allowed list. Never invent a class.
- `evidence` must quote or name specific input fields you relied on. Do not
  cite a field that was not provided.
- `confidence` is your calibrated probability that the class is correct. Be
  honest: if the structured fields conflict, say so and lower it.
- If the input genuinely does not determine a class, return UNKNOWN with low
  confidence rather than guessing a specific one.

Important domain context:
- `insufficient_funds` on a SUBSCRIPTION/mandate debit is MANDATE_INSUFFICIENT,
  not INSUFFICIENT_FUNDS. The mandate is alive; the debit bounced. Recovery is
  re-presentment, which is a different action from nudging a customer.
- `gateway_technical_error` inside a detected outage window is ISSUER_DOWN.
  The same error with `inside_detected_outage: false` is more likely an
  isolated GATEWAY_TIMEOUT.
- A high `customer_success_ratio` with a sudden hard decline suggests an
  instrument problem, not an intent problem.

Return ONLY a JSON object, no prose around it:
{"failure_class": "<CLASS>", "confidence": <0..1>, "evidence": ["..."], "reasoning": "<one or two sentences>"}"""


def _prompt_for(facts: RiskFacts) -> str:
    return (
        "Allowed classes: "
        + ", ".join(ALLOWED_CLASSES)
        + "\n\nFailed payment:\n"
        + json.dumps(facts.to_prompt_dict(), indent=2, sort_keys=True)
    )


def _hash_prompt(system: str, user: str) -> str:
    return hashlib.sha256((system + "\x00" + user).encode()).hexdigest()[:16]


# Anthropic list prices (USD per million tokens) for the models we may use,
# converted at a fixed rate so the cost column is reproducible across runs.
USD_PER_INR = 1 / 88.0
PRICES_USD_PER_MTOK = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-5": (15.0, 75.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


def _cost_inr(model: str, in_tok: int, out_tok: int) -> float:
    pin, pout = PRICES_USD_PER_MTOK.get(model, (3.0, 15.0))
    usd = (in_tok / 1e6) * pin + (out_tok / 1e6) * pout
    return round(usd / USD_PER_INR, 6)


class Diagnoser:
    """Tiered diagnoser.

    `mode`:
      live  -- call the Anthropic API for ambiguous cases (requires a key)
      stub  -- deterministic offline stand-in, so the pipeline and the
               dashboard run with no key and no network. Stub runs are stamped
               as such in the ledger and the report; headline metrics are only
               ever quoted from a live run.
    """

    def __init__(self, mode: str = "auto", model: Optional[str] = None):
        self.model = model or os.getenv("RECOUP_MODEL", "claude-sonnet-5")
        key = os.getenv("ANTHROPIC_API_KEY")
        if mode == "auto":
            mode = "live" if key else "stub"
        if mode == "live" and not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set; use mode='stub'")
        self.mode = mode
        self._client = None
        self.stats = {
            "deterministic": 0,
            "llm": 0,
            "fallback": 0,
            "llm_cost_inr": 0.0,
            "in_tokens": 0,
            "out_tokens": 0,
            "schema_violations": 0,
            "api_errors": 0,
        }
        if self.mode == "live":
            from anthropic import Anthropic

            self._client = Anthropic(api_key=key)

    # -- tier routing -------------------------------------------------------
    def _needs_llm(self, facts: RiskFacts) -> Tuple[bool, str]:
        reason = facts.error_reason or ""
        if reason in AMBIGUOUS_REASONS:
            return True, f"'{reason}' is context-dependent"
        if reason not in DETERMINISTIC_MAP:
            return True, f"no deterministic mapping for '{reason}'"
        if facts.in_degradation and reason in ("gateway_timeout",):
            return True, "error contradicts the detected outage context"
        if facts.kind == "B2B_INVOICE":
            return True, "B2B collectability is a judgement, not a code lookup"
        return False, ""

    def _tier1(self, facts: RiskFacts) -> FailureClass:
        reason = facts.error_reason or ""
        if reason in DETERMINISTIC_MAP:
            return DETERMINISTIC_MAP[reason]
        # Best-effort fallback used when the LLM is unavailable.
        if facts.in_degradation:
            return FailureClass.ISSUER_DOWN
        if reason == "insufficient_funds":
            return (
                FailureClass.MANDATE_INSUFFICIENT
                if facts.kind == "SUBSCRIPTION"
                else FailureClass.INSUFFICIENT_FUNDS
            )
        if reason == "gateway_technical_error":
            return FailureClass.ISSUER_DOWN
        return FailureClass.UNKNOWN

    # -- main entry point ---------------------------------------------------
    def diagnose(self, facts: RiskFacts) -> Diagnosis:
        need, why = self._needs_llm(facts)
        if not need:
            self.stats["deterministic"] += 1
            cls = self._tier1(facts)
            return Diagnosis(
                order_id=facts.order_id,
                failure_class=cls,
                confidence=0.97,
                evidence=[f"error_reason={facts.error_reason}"],
                reasoning="Unambiguous error_reason; resolved by deterministic map "
                          "without invoking a model.",
                tier="deterministic",
            )

        user = _prompt_for(facts)
        ph = _hash_prompt(SYSTEM_PROMPT, user)

        if self.mode == "stub":
            self.stats["llm"] += 1
            cls = self._tier1(facts)
            return Diagnosis(
                order_id=facts.order_id,
                failure_class=cls,
                confidence=0.75,
                evidence=[f"error_reason={facts.error_reason}", f"kind={facts.kind}"],
                reasoning=f"STUB MODE (no API key): {why}. Deterministic stand-in used.",
                tier="llm",
                prompt_hash=ph,
                model_version="stub",
            )

        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            )
            in_tok = resp.usage.input_tokens
            out_tok = resp.usage.output_tokens
            cost = _cost_inr(self.model, in_tok, out_tok)
            self.stats["llm"] += 1
            self.stats["in_tokens"] += in_tok
            self.stats["out_tokens"] += out_tok
            self.stats["llm_cost_inr"] += cost

            raw = resp.content[0].text.strip()
            data = _extract_json(raw)
            # Validate hard. An unparseable or out-of-taxonomy answer is not
            # silently coerced -- it is recorded as a schema violation and the
            # deterministic tier answers instead.
            d = Diagnosis(
                order_id=facts.order_id,
                failure_class=FailureClass(data["failure_class"]),
                confidence=float(data.get("confidence", 0.5)),
                evidence=list(data.get("evidence", []))[:6],
                reasoning=str(data.get("reasoning", ""))[:500],
                tier="llm",
                cost_inr=cost,
                input_tokens=in_tok,
                output_tokens=out_tok,
                prompt_hash=ph,
                model_version=self.model,
            )
            return d
        except (KeyError, ValueError, ValidationError, TypeError) as e:
            self.stats["schema_violations"] += 1
            self.stats["fallback"] += 1
            return Diagnosis(
                order_id=facts.order_id,
                failure_class=self._tier1(facts),
                confidence=0.55,
                evidence=[f"error_reason={facts.error_reason}"],
                reasoning="Model returned an unusable payload; deterministic tier used.",
                tier="fallback",
                prompt_hash=ph,
                model_version=self.model,
                fallback_reason=f"{type(e).__name__}: {e}",
            )
        except Exception as e:  # network, rate limit, overload
            self.stats["api_errors"] += 1
            self.stats["fallback"] += 1
            return Diagnosis(
                order_id=facts.order_id,
                failure_class=self._tier1(facts),
                confidence=0.5,
                evidence=[f"error_reason={facts.error_reason}"],
                reasoning="Model call failed; deterministic tier used so the batch "
                          "continues rather than stalling.",
                tier="fallback",
                prompt_hash=ph,
                model_version=self.model,
                fallback_reason=f"{type(e).__name__}: {str(e)[:200]}",
            )


def _extract_json(raw: str) -> Dict:
    """Pull the JSON object out of a model response.

    Tolerates a fenced block or surrounding prose, because being strict about
    formatting while the content is correct would just throw away good answers.
    """
    t = raw.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    t = t.strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        raise ValueError(f"no JSON object in response: {raw[:120]!r}")
    return json.loads(t[i : j + 1])


def score_diagnosis(
    diagnoses: Dict[str, Diagnosis],
    ground_truth: Dict[str, "object"],
) -> Dict:
    """Per-tier and per-class accuracy against the hidden true class.

    Split by tier on purpose: a headline "91% accurate" is meaningless if the
    deterministic tier carries 70% of volume at 99% and the model is at 62% on
    the hard remainder. The panel will ask; the number should already be there.
    """
    from collections import defaultdict

    per_tier: Dict[str, List[bool]] = defaultdict(list)
    per_class: Dict[str, Dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    conf_bins: Dict[str, List[bool]] = defaultdict(list)

    for oid, d in diagnoses.items():
        gt = ground_truth.get(oid)
        if gt is None:
            continue
        true_cls = gt.true_class.value
        pred = d.failure_class.value
        ok = pred == true_cls
        per_tier[d.tier].append(ok)
        if ok:
            per_class[true_cls]["tp"] += 1
        else:
            per_class[pred]["fp"] += 1
            per_class[true_cls]["fn"] += 1
        # Calibration: does stated confidence track actual accuracy?
        b = f"{int(d.confidence * 10) * 10}-{int(d.confidence * 10) * 10 + 10}%"
        conf_bins[b].append(ok)

    tiers = {
        t: {
            "n": len(v),
            "accuracy": round(sum(v) / len(v), 4) if v else None,
        }
        for t, v in per_tier.items()
    }
    all_ok = [o for v in per_tier.values() for o in v]
    classes = {}
    for c, m in sorted(per_class.items()):
        tp, fp, fn = m["tp"], m["fp"], m["fn"]
        classes[c] = {
            "support": tp + fn,
            "precision": round(tp / (tp + fp), 3) if (tp + fp) else None,
            "recall": round(tp / (tp + fn), 3) if (tp + fn) else None,
        }
    return {
        "overall_accuracy": round(sum(all_ok) / len(all_ok), 4) if all_ok else None,
        "n": len(all_ok),
        "by_tier": tiers,
        "by_class": classes,
        "calibration": {
            b: {"n": len(v), "actual_accuracy": round(sum(v) / len(v), 3)}
            for b, v in sorted(conf_bins.items())
        },
    }
