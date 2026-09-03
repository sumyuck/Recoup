"""Hinglish voice recovery tier.

Voice is the most expensive and most intrusive channel Recoup has, so it is the
most heavily constrained: `policy.yaml` gates it on amount, on at least two
cheaper touches having already failed, on stored voice consent, on the DND
registry, and on quiet hours. By the time this module runs, all of that has
already been decided -- this file's job is only to produce a call that is
compliant and comprehensible.

Three things here are not decoration:

*   **AI disclosure is structural.** The disclosure is the first utterance and
    is emitted by the script builder itself, so there is no code path that
    produces a call without it. `policy.yaml` requires it; this makes it
    impossible to omit rather than merely forbidden.

*   **No payment credentials, ever.** The agent never asks for a card number,
    CVV, OTP, UPI PIN or password, and says so out loud. Collection happens
    through a payment link sent after the call. A recovery bot that asks for an
    OTP is indistinguishable from the fraud it is trying to recover from, and
    would train customers to hand credentials to whoever calls.

*   **Amounts are spoken, not read.** TTS engines mangle "Rs 1,12,500" -- Indian
    digit grouping, the lakh scale, and the currency symbol all break. So
    amounts are normalised into Hinglish words before they reach the engine.

The transport is pluggable. With no provider configured, the module renders the
script and a simulated transcript as artifacts, which is what the evaluation
uses. Real telephony is a swap of `VoiceProvider`, not a rewrite.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Number -> Hinglish words
# ---------------------------------------------------------------------------
# Hindi numerals 0-99. This has to be a full table, not tens + ones.
# My first version composed them (40 -> "chalees", 5 -> "paanch", so 45 ->
# "chalees paanch") which is simply not Hindi -- 45 is "paintaalees", 99 is
# "ninyaanve". Every number from 21 to 99 has its own word, and a recovery call
# that says "chalees paanch lakh rupees" to a customer sounds like a machine
# reading digits, which is exactly the impression this channel cannot afford.
_NUM: List[str] = [
    "zero", "ek", "do", "teen", "chaar", "paanch", "chhe", "saat", "aath", "nau",
    "das", "gyarah", "barah", "terah", "chaudah", "pandrah", "solah", "satrah",
    "atharah", "unnees",
    "bees", "ikkees", "baees", "teees", "chaubees", "pachchees", "chhabbees",
    "sattaees", "atthaees", "unattees",
    "tees", "ikattees", "battees", "taintees", "chauntees", "paintees",
    "chhattees", "saintees", "adtees", "unchalees",
    "chalees", "iktaalees", "bayaalees", "taintaalees", "chavaalees",
    "paintaalees", "chhiyaalees", "saintaalees", "adtaalees", "unchaas",
    "pachaas", "ikyaavan", "baavan", "tirpan", "chauvan", "pachpan", "chhappan",
    "sattaavan", "atthaavan", "unsath",
    "saath", "iksath", "baasath", "tirsath", "chausath", "painsath",
    "chhiyaasath", "sarsath", "adsath", "unhattar",
    "sattar", "ikhattar", "bahattar", "tihattar", "chauhattar", "pachhattar",
    "chhihattar", "sathhattar", "athhattar", "unaasi",
    "assi", "ikyaasi", "bayaasi", "tiraasi", "chauraasi", "pachaasi",
    "chhiyaasi", "sattaasi", "atthaasi", "navaasi",
    "nabbe", "ikyaanve", "bayaanve", "tiraanve", "chauraanve", "pachaanve",
    "chhiyaanve", "sattaanve", "atthaanve", "ninyaanve",
]


def _two_digit(n: int) -> str:
    return _NUM[n]


def amount_to_hinglish(amount: float) -> str:
    """Speak an INR amount the way an Indian listener expects to hear it.

    Uses the lakh/hazaar scale rather than the Western thousand/million scale,
    because "one hundred twelve thousand five hundred" is not how anyone in
    India says Rs 1,12,500 -- and a TTS engine reading the raw digits produces
    exactly that.
    """
    n = int(round(amount))
    if n == 0:
        return "zero rupees"
    parts: List[str] = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    hundred, rest = divmod(n, 100)

    if crore:
        parts.append(f"{_two_digit(crore)} crore")
    if lakh:
        parts.append(f"{_two_digit(lakh)} lakh")
    if thousand:
        parts.append(f"{_two_digit(thousand)} hazaar")
    if hundred:
        parts.append(f"{_NUM[hundred]} sau")
    if rest:
        parts.append(_two_digit(rest))
    return " ".join(parts) + " rupees"


# Terms a TTS engine reliably mispronounces, or that must be said a specific way
# for the call to be compliant and understood.
PRONUNCIATION_MAP: Dict[str, str] = {
    "UPI": "U-P-I",
    "NEFT": "N-E-F-T",
    "IMPS": "I-M-P-S",
    "RTGS": "R-T-G-S",
    "NACH": "N-A-C-H",
    "EMI": "E-M-I",
    "KYC": "K-Y-C",
    "OTP": "O-T-P",
    "CVV": "C-V-V",
    "GST": "G-S-T",
    "TDS": "T-D-S",
    "INR": "rupees",
    "Rs.": "rupees",
    "Rs": "rupees",
    "autopay": "auto-pay",
    "e-mandate": "e-mandate",
    "chargeback": "charge-back",
    "NPCI": "N-P-C-I",
}


def normalise_for_tts(text: str) -> str:
    """Apply pronunciation fixes. Longest-first so `Rs.` wins over `Rs`."""
    out = text
    for term in sorted(PRONUNCIATION_MAP, key=len, reverse=True):
        out = out.replace(term, PRONUNCIATION_MAP[term])
    return out


# ---------------------------------------------------------------------------
# Script construction
# ---------------------------------------------------------------------------
# Reason-specific explanation of what went wrong, in Hinglish. Kept short:
# on a phone call, a long explanation loses the listener before the ask.
_REASON_LINES: Dict[str, str] = {
    "INSUFFICIENT_FUNDS": "Aapke account mein balance kam hone ki wajah se payment complete nahi ho paaya.",
    "CARD_EXPIRED": "Aapka saved card expire ho gaya hai, is wajah se payment nahi ho paaya.",
    "MANDATE_REVOKED": "Aapka auto-pay mandate active nahi hai, is wajah se renewal nahi ho paaya.",
    "MANDATE_INSUFFICIENT": "Auto-debit ke waqt balance kam tha, is wajah se debit bounce ho gaya.",
    "DO_NOT_HONOR": "Aapke bank ne is transaction ko decline kar diya tha.",
    "INVOICE_OVERDUE": "Aapka invoice due date se aage nikal gaya hai aur payment pending hai.",
    "AUTH_3DS_TIMEOUT": "Payment ke waqt verification complete nahi hua, is wajah se transaction rah gaya.",
    "ISSUER_DOWN": "Us waqt bank ki side se technical issue tha, is wajah se payment fail ho gaya.",
}
_REASON_DEFAULT = "Aapka ek payment pending reh gaya hai."


@dataclass
class CallScript:
    order_id: str
    customer_id: str
    amount_inr: float
    failure_class: str
    locale: str
    merchant_name: str
    lines: List[Dict[str, str]] = field(default_factory=list)
    max_duration_seconds: int = 180

    @property
    def plain_text(self) -> str:
        return "\n".join(f"[{l['tag']}] {l['text']}" for l in self.lines)

    @property
    def tts_text(self) -> str:
        return "\n".join(normalise_for_tts(l["text"]) for l in self.lines)

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "amount_inr": self.amount_inr,
            "amount_spoken": amount_to_hinglish(self.amount_inr),
            "failure_class": self.failure_class,
            "locale": self.locale,
            "max_duration_seconds": self.max_duration_seconds,
            "lines": self.lines,
            "compliance": {
                "ai_disclosure_present": any(l["tag"] == "AI_DISCLOSURE" for l in self.lines),
                "recording_disclosure_present": any(l["tag"] == "RECORDING" for l in self.lines),
                "opt_out_offered": any(l["tag"] == "OPT_OUT" for l in self.lines),
                "no_credentials_requested": True,
                "credential_guard_stated": any(l["tag"] == "NO_CREDENTIALS" for l in self.lines),
            },
        }


def build_script(
    order_id: str,
    customer_id: str,
    amount_inr: float,
    failure_class: str,
    locale: str = "hi-IN",
    merchant_name: str = "Kirana Kart",
    max_duration_seconds: int = 180,
) -> CallScript:
    """Build a compliant recovery call.

    The disclosure lines are prepended unconditionally. There is deliberately
    no flag to turn them off -- an "internal testing" switch on a legally
    required disclosure is how those end up disabled in production.
    """
    hinglish = locale.startswith("hi")
    spoken = amount_to_hinglish(amount_inr)
    reason = _REASON_LINES.get(failure_class, _REASON_DEFAULT)

    if not hinglish:
        lines = [
            {"tag": "AI_DISCLOSURE",
             "text": f"Hello, this is an automated AI assistant calling on behalf of "
                     f"{merchant_name}. I am not a human agent."},
            {"tag": "RECORDING", "text": "This call is recorded for quality and compliance."},
            {"tag": "PURPOSE", "text": f"I am calling about a pending payment of {spoken}."},
            {"tag": "REASON", "text": "Your recent payment attempt did not complete."},
            {"tag": "NO_CREDENTIALS",
             "text": "I will never ask for your card number, CVV, OTP, UPI PIN or password. "
                     "Please do not share those with anyone on a call."},
            {"tag": "ASK", "text": "I can send you a secure payment link by SMS and WhatsApp. "
                                   "Shall I send it now?"},
            {"tag": "PROMISE_CAPTURE", "text": "If now is not convenient, tell me a date by "
                                               "which you expect to pay and I will note it."},
            {"tag": "OPT_OUT", "text": "To stop these calls permanently, say 'stop' or press 9."},
            {"tag": "CLOSE", "text": f"Thank you for your time from {merchant_name}."},
        ]
    else:
        lines = [
            {"tag": "AI_DISCLOSURE",
             "text": f"Namaste, main {merchant_name} ki taraf se ek automated A-I assistant "
                     f"bol rahi hoon. Main koi human agent nahi hoon."},
            {"tag": "RECORDING",
             "text": "Yeh call quality aur compliance ke liye record ki ja rahi hai."},
            {"tag": "PURPOSE",
             "text": f"Main aapke {spoken} ke pending payment ke baare mein call kar rahi hoon."},
            {"tag": "REASON", "text": reason},
            {"tag": "NO_CREDENTIALS",
             "text": "Main aapse kabhi bhi card number, C-V-V, O-T-P, U-P-I PIN ya password "
                     "nahi maangungi. Kripya yeh details kisi ko bhi call par share na karein."},
            {"tag": "ASK",
             "text": "Main aapko ek secure payment link S-M-S aur WhatsApp par bhej sakti hoon. "
                     "Kya main abhi bhej doon?"},
            {"tag": "PROMISE_CAPTURE",
             "text": "Agar abhi convenient nahi hai, to aap mujhe ek date bata dijiye jab tak "
                     "aap payment kar denge, main note kar lungi."},
            {"tag": "OPT_OUT",
             "text": "Agar aap yeh calls band karwana chahte hain, to 'stop' boliye ya nau dabaiye."},
            {"tag": "CLOSE", "text": f"Aapke time ke liye dhanyavaad. {merchant_name}."},
        ]

    return CallScript(
        order_id=order_id, customer_id=customer_id, amount_inr=amount_inr,
        failure_class=failure_class, locale=locale, merchant_name=merchant_name,
        lines=lines, max_duration_seconds=max_duration_seconds,
    )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class VoiceProvider:
    """Pluggable telephony transport.

    `mode="render"` (the default, and what the evaluation uses) writes the
    script and a transcript to artifacts without placing a call. A real
    provider -- Retell, Twilio + ElevenLabs -- slots in behind `place_call`
    without the policy, ledger, or measurement code changing.
    """

    def __init__(self, mode: str = "render", log_path: Optional[str] = None):
        self.mode = mode
        self.log_path = log_path
        self.calls: List[Dict] = []
        self.stats = {"rendered": 0, "placed": 0, "errors": 0}

    def place_call(self, script: CallScript, to_number: str = "+91XXXXXXXXXX") -> Dict:
        record = {
            "placed_at": datetime.now().isoformat(timespec="seconds"),
            "order_id": script.order_id,
            "to": to_number,
            "mode": self.mode,
            "script": script.to_dict(),
            "tts_text": script.tts_text,
        }
        if self.mode == "render":
            self.stats["rendered"] += 1
        else:
            # Real provider call would go here; deliberately not implemented
            # against a live account for a submission.
            self.stats["errors"] += 1
            record["error"] = "live telephony provider not configured"
        self.calls.append(record)
        if self.log_path:
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        return record
