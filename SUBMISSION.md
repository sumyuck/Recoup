# Submission form — draft answers

The live form is at https://forms.gle/d9r2gvxp8cmoZhon9 (*"Razorpay AI Builder -
Registration Form"*). **It is the final submission and cannot be edited after
submitting** — it requires the GitHub URL and the video link as mandatory fields, so it
goes in last, with about two hours of buffer.

Fields it asks for:

| field | value |
|---|---|
| Full Name | *(yours)* |
| College Name | *(yours)* |
| Graduation Year | 2027 / 2028 / 2029 — **only these three are accepted** |
| In-person availability from September, Bangalore | Yes |
| Preferred Duration | 6-Month / 12-Month |
| Selected Track | **Track 3: AI Revenue Recovery** |
| Project Name / Title | **Recoup — a revenue-recovery agent that proves how much money it actually made** |
| Project Objectives | *draft below* |
| GitHub Repository URL | *(public repo URL)* |
| 5-min Pitch Video Link | *(unlisted YouTube / Drive link — check it plays signed-out)* |
| Build Challenges & Technical Obstacles | *draft below* |
| Final Submission Confirmation | tick only when the repo is public and the video plays |

---

## Project Objectives — *"What does it solve?"*

> When a payment fails, most merchants retry it three times on a fixed schedule and hope.
> Recoup replaces that with an agent that diagnoses *why* each failure happened, chooses a
> bounded recovery action, and — critically — measures whether it actually worked.
>
> The problem I set out to solve is not "recover more payments". It is that **nobody can
> tell whether a dunning system works.** Around a quarter of failed payments recover on
> their own: an issuer outage ends, a salary lands, a customer retries unprompted. Any
> system that contacts everyone gets to report that as its own recovery. So the headline
> number every tool in this category quotes is largely fiction.
>
> Recoup assigns 20% of orders to a randomized, stratified holdout that is **never
> contacted**, and only ever claims the difference. On a 500-order batch that is
> **+24.4 percentage points of incremental recovery, ₹10.3L, 95% CI +14.8 to +33.4**, at
> **₹0.50 per ₹100 recovered**. Against the naive retry schedule a merchant already runs
> — the fair commercial comparison, not against nothing — it is **+19.7pp**. It also
> reports what a recovery rate hides: contacts wasted on customers who were going to pay
> anyway, and the permanent opt-outs the campaign caused, priced as forward revenue lost.
>
> Architecturally the decision that matters is that **the LLM proposes and a deterministic
> policy engine authorizes.** `policy.yaml` is the entire authorization surface for money
> and contact actions — retry caps, contact frequency, quiet hours, DND, permanent
> opt-out, consent, a voice tier gated on amount and on two cheaper touches having already
> failed, human approval above ₹75,000, and an economic stop that does nothing when
> expected value is under 1.5× the cost of acting. There is no override path in code, so a
> hallucinating or prompt-injected model cannot do more than get denied — and the denial is
> logged with the rule that caught it. Every decision lands in an append-only hash-chained
> ledger, so any order's full history is replayable and tamper-evident.
>
> It runs on Razorpay test-mode APIs and ingests real signed Razorpay webhooks
> (`payment.failed`, `subscription.charged`, `subscription.halted`, `invoice.expired`,
> `payment_link.expired`), with HMAC-SHA256 signature verification and replay protection.
> The recovery ladder includes a Hinglish voice tier with structural AI disclosure that
> never asks for a card number, OTP or UPI PIN.

*(~330 words. Trim the last paragraph first if the field is short.)*

---

## Build Challenges & Technical Obstacles

> *"What issues did you face while building, and how did you solved them?"*

This is effectively an interview question. Specific, named bugs beat polish. All fourteen
are logged in `ARCHITECTURE.md`; these four are the ones that changed the result.

> **1. My detector reported five incidents for two real outages, and the false positives
> were not false positives.** I sliced traffic by (issuer × method) and (gateway × method)
> and tested each hourly cell against its own trailing baseline. Precision came out at
> 0.4. When I looked at the extra signals, they were real: an ICICI outage suppresses
> netbanking on *every* gateway, so the same root cause was firing on both slices. The fix
> was root-cause attribution rather than a tighter threshold — among signals overlapping in
> time on the same method, the largest effect size is the incident and the rest are
> recorded as its shadows, kept in the audit trail but collapsed in the incident list.
> Separately I was running ~3,500 hypothesis tests at α=0.01, which manufactures ~35
> incidents from noise alone, so α is Bonferroni-corrected to 2.8e-6. Precision and recall
> are now both 1.0 across seven seeds — and I measured where that breaks: recall holds to
> ~35% outage severity, then collapses to 0.10 at 15%.
>
> **2. My false-positive cost came out as exactly zero, which is impossible.** Contacts
> sent to customers who would have paid anyway are the main hidden cost of dunning, and my
> report said there were none. The cause was in the simulator: self-healing orders resolved
> at step 0, before the agent could contact them, so a wasted contact could never exist.
> Real systems cannot tell in advance which failures will heal — you contact them and
> *then* they pay. I gave the organic-recovery channel a realistic arrival time so it
> competes with the agent on a timeline. My headline lift got **worse**, from 35.9pp to
> 34.4pp, which is how I knew the fix was right.
>
> **3. 85 recoverable orders were being abandoned because I treated every policy refusal as
> final.** An order blocked by a customer's 24-hour contact cap was marked
> `NO_ELIGIBLE_ACTION` and never revisited — the cap clears the next day. Worse, 51 orders
> that needed human review vanished silently: the escalation rule fired while I was
> *probing* candidate actions, so the real gate never ran and no escalation event was ever
> emitted. Orders in dispute or on legal hold were being dropped rather than escalated. I
> introduced a denial taxonomy — TRANSIENT / HUMAN / TERMINAL — with a computed re-wake
> time for transient refusals and an explicit human queue that is part of the reported
> output. I also found that probing an expensive voice call was tripping the batch budget
> halt and killing the rest of the run, because a dry-run probe was mutating engine state.
>
> **4. The most useful bug: my first live run showed the LLM making things worse, and it
> was my fault twice over.** Diagnosis accuracy on the ambiguous slice came back at 63.8%
> against a 77% deterministic fallback. The confusion matrix showed all 43 errors were two
> pairs. My system prompt told the model that `gateway_technical_error` outside a detected
> outage was "likely GATEWAY_TIMEOUT" — wrong, that reason is always issuer-side in
> Razorpay's taxonomy — and told it to split `insufficient_funds` on order kind when the
> real discriminator is `error_source` (`customer` = short balance at checkout, `bank` =
> bounced auto-debit against a live mandate). I was actively misleading it. A third of the
> "API errors" were an `AttributeError` in my own response parsing that I had
> misattributed to the provider. Fixing all three took accuracy to 100% — **and that
> result was itself a problem.** 100% meant the task was fully determined by the
> structured fields, so the model was doing work a dict lookup already did, and arm C
> could never beat arm B. My corpus was too tidy. So I added field noise that mirrors real
> gateways — dropped `error_reason`, vendor-specific codes in no taxonomy, misattributed
> `error_source` — while keeping the cause recoverable from the free-text description. On
> that corpus the deterministic tier degrades and the model does not, and the model's
> contribution is finally measurable: **+5.5pp of additional lift, ₹68k more recovered,
> and fewer wasted contacts and opt-outs.** The honest conclusion is conditional: on tidy
> data the model is not worth its latency or cost; it earns its place exactly where the
> structured fields stop being trustworthy.

*(~640 words. If the field is short, keep #4 and #2 — they are the two that show
judgement rather than effort.)*

---

## Pre-submit checklist

- [ ] `make demo` passes from a clean clone (`git clone` to /tmp, fresh venv)
- [ ] Repo is **public**; `.env` is gitignored and contains no real keys
- [ ] `git log` reads as a build, not one squashed commit
- [ ] README leads with the numbers table and states the limitations honestly
- [ ] `RESULTS.md` regenerated from the final `make ablation` run
- [ ] Video is ≤5:00, plays signed-out, audio audible, terminal text legible
- [ ] Graduation year is 2027/2028/2029
- [ ] Only submit the form once, ~2h before the deadline
