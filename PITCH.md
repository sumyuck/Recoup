# 5-minute pitch — script and shot list

Rules I'm holding myself to: no slides except one architecture frame, no music, screen
recording with my own voice, and **no more than 25 seconds on the problem statement.**
Judges know what a failed payment is.

Numbers below are frozen from the final live run in `RESULTS.md`. If the evaluation is
rerun, update this script and the submission copy together before recording.

---

## Before recording

```bash
make demo                       # calibrate → eval → chaos, all fresh
make eval-live                  # the run you quote on camera
make serve                      # dashboard on :8000
```

Open three things and nothing else: the dashboard, a terminal, and `policy.yaml`.
Close Slack. Full-screen the browser. Zoom the terminal font to ~16pt so it's readable
when compressed.

---

## 0:00–0:25 — the problem, in numbers, then stop

> "When a payment fails, most merchants retry it three times on a fixed schedule and
> hope. Some of that money comes back on its own. So when a dunning tool tells you it
> recovered 40% of failed payments, you have no idea whether it did anything at all.
>
> I built a recovery agent that answers that question about itself."

**Shot:** terminal, nothing running yet. Do not show a slide. Do not explain what
dunning is.

---

## 0:25–1:10 — the run

```bash
make eval-live
```

Talk over it while it streams:

> "Five hundred at-risk orders — failed payments, bounced autopay debits, overdue B2B
> invoices — plus two hundred thousand background traffic events so the detector has
> real statistics to work on.
>
> Three arms on the same corpus. Arm A is the fixed retry schedule a merchant already
> runs. Arm B is good heuristics with no model anywhere. Arm C is the full agent.
>
> And twenty percent of orders go into a holdout that is **never contacted at all.**"

**Shot:** the arm table filling in. Let it finish. Don't narrate every column.

---

## 1:10–2:10 — the one idea (the most important minute)

Point at the holdout column.

> "Here's why the holdout matters. The holdout recovered **23.2% — 23 of 99 orders** while
> being completely ignored — an outage ended, a salary landed, someone retried on their
> own. That recovery has nothing to do with any agent.
>
> Any system that touches everything gets to claim that number. Mine can't, because I
> deliberately withheld a control group.
>
> So the only thing I claim is the difference: **+31.4 percentage points, ₹10.35L
> incremental, 95% confidence interval +22.0 to +40.5.** Bootstrap, not a
> point estimate — and if that interval crossed zero the report would say *not
> significant.*"

Then the honest downgrade:

> "And the number a merchant actually buys is smaller than that. Against arm A — the
> retry schedule they already have, not against nothing — it's **+26.7 points** and
> **₹7.04L.** That's the real offer, so that's the number I lead with."

**Shot:** the three KPI cards, then the "lift vs merchant baseline" card.

---

## 2:10–3:00 — one order's full audit trail

Open order `order_5df8aa203d33da`, a ₹5,720 failed mandate debit.

> "Every decision is in a hash-chained ledger. This is one invoice, end to end.
>
> Detection is a two-proportion z-test — deterministic, no model, because 'is 61% lower
> than 94%' is arithmetic and paying a model to do arithmetic would make my numbers
> unreproducible.
>
> Diagnosis is where the model earns its place: this one said `insufficient_funds`, but
> `error_source=bank` and the live e-mandate show that it is a bounced auto-debit, not a
> customer-side checkout failure. The model classified it as `MANDATE_INSUFFICIENT` and
> chose mandate re-presentment first.
>
> After two attempts, the next communication landed in quiet hours. **The policy engine
> refused it**, named the exact rule and deferred the sequence instead of abandoning it.
> It came back at 9:00, sent the permitted SMS and recovered the payment."

**Shot:** the drawer timeline. Scroll slowly through the green ✓ / red ✗ rule trace.
**This is the money shot of the whole video.** Do not rush it.

---

## 3:00–3:35 — bounded, and why that's the architecture

Switch to `policy.yaml`.

> "This file is the entire authorization surface. The model proposes; this decides.
> There is no override path in code — so a hallucinating or prompt-injected model can't
> do more than get denied, and the denial is logged with the rule that caught it.
>
> Contact caps, quiet hours, DND, permanent opt-out, voice gated on amount and on two
> cheaper touches having already failed, human approval above ₹75,000, and an **economic
> stop**: if expected value is under 1.5× the cost of acting, doing nothing is the
> correct answer.
>
> Every verdict logs the full rule trace on **allow** as well as deny — because 'why was
> this permitted?' is the question you actually get asked in an audit."

**Shot:** scroll `policy.yaml`. It reads as a document, which is the point.

---

## 3:35–4:15 — failure path, live

```bash
make chaos
```

> "Injected gateway failure at 0, 15, 35 and 60 percent. In the final 60% run: **87**
> transient errors, **49** ambiguous timeouts, **4** circuit-breaker trips and **50**
> dead-lettered — while double charges stay at zero.
>
> The important one is ambiguous timeouts. A timeout means *unknown*, not *failed*.
> Retrying blind is exactly how a customer gets charged twice, so it reconciles true
> state first.
>
> And this — three identical submissions of the same charge, the way a retried webhook
> or a double-clicked button actually arrives. **One gateway call. Two duplicates
> prevented. Zero double charges.**"

**Shot:** the chaos table, then the duplicate-submission proof block.

---

## 4:15–4:45 — what I got wrong

Do not skip this. It is the highest-value 30 seconds in the video.

> "Three things I got wrong that changed the result.
>
> My detector reported five incidents for two real outages. Precision 0.4. They weren't
> false positives — an ICICI outage dips *every* gateway, so the same root cause was
> firing on both slices. I added attribution: largest effect size wins, the rest are
> recorded as shadows. Two incidents, precision 1.0.
>
> My false-positive cost came out as exactly zero, which is impossible. Self-healing
> orders were resolving before the agent could contact them, so wasted contacts could
> never be counted. Fixed it, and my lift number **got worse** — which is how I knew the
> fix was right.
>
> And my first run showed 42-point lift and 99% of the recovery ceiling. That's not a
> good result, it's a broken simulator. I scaled my own effectiveness estimates down by
> half. All fourteen bugs are in ARCHITECTURE.md."

**Shot:** the bug table in ARCHITECTURE.md.

---

## 4:45–5:00 — limits and close

> "What's real: the measurement machinery, the policy engine, the audit trail, and the
> Razorpay integration — it ingests real signed `payment.failed` and
> `subscription.halted` webhooks. What's simulated: whether a customer actually pays.
>
> So the rupee figure is synthetic. The method isn't — point it at real Razorpay data
> and the holdout, the confidence intervals and the exception list all still work.
>
> That's Recoup."

Stop. No "thank you for your time" slide.

---

## Things to have ready for the panel

They will ask these. Have the answer in one sentence each.

| question | answer |
|---|---|
| Why deterministic detection, not an LLM? | It's a hypothesis test over counts — exact, reproducible, free. Non-determinism there would poison every downstream measurement. |
| Why not LangChain / an agent framework? | I needed a deterministic gate and a replayable audit log. A hand-rolled state machine means I can explain every line and the ledger *is* the state. |
| Why 3 retries and not 5? | `policy.yaml`, and it's the wrong question to answer from taste — the economic stop already ends sequences that aren't worth continuing, so the cap is a blast-radius limit, not a tuning knob. |
| Why cap contacts at 1 per 24h? | Opt-outs are permanent and priced in the report at ₹14,628 of forward revenue. Over-contacting is the failure mode a dunning system hides best. |
| Your lift looks high. | Against a no-contact holdout, yes. Against the retry schedule a merchant already runs it's +26.7pp, and I scaled my own effectiveness estimates down 48% after the first run gave an implausible 42. |
| What breaks first in production? | Detection on low-volume cells — 1,447 cell-buckets were too sparse to test, and recall falls to 0.10 at 15% outage severity. Measured, in the sweep artifact. |
| Where does the LLM actually help? | The ambiguous slice only, 144 of 327 diagnosed orders. Reported as a separate tier accuracy so it has to earn it. |
