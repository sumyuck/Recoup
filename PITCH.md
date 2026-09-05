# Recoup — final live-demo pitch

Target runtime: **4:20–4:40**. Record the running product with your own voice. The
generated video in `docs/` is only the emergency fallback.

The numbers in this script are frozen from `RESULTS.md`. Do not rerun the live model
while recording: it adds latency and risk without proving anything the dashboard and
reproducible artifacts do not already prove.

## One-minute setup

```bash
make pitch
```

Open `http://127.0.0.1:8000` in a clean browser window. Use 1920×1080, browser zoom
90–100%, a large cursor, and no notifications. Keep one terminal ready in a second
window with these commands typed but not run:

```bash
.venv/bin/python cli.py trace order_5df8aa203d33da
.venv/bin/python cli.py verify artifacts/ledger_C_AGENT.jsonl
```

Record with macOS Screenshot (`Shift-Command-5`) or OBS, microphone on. Speak a little
slower than normal. Do one complete take before trying to perfect individual lines.

## 0:00–0:35 — open on the uncomfortable number

**Shot:** Dashboard hero. Keep the three numbers visible.

> “A normal recovery dashboard would say this system recovered **₹13.76 lakh**. That is
> the flattering number — and it is not the number I trust.
>
> Some failed payments recover on their own. A merchant also already has a retry
> schedule. So I built Recoup: a bounded recovery agent that has to prove how much
> money it caused, decide the next best intervention, and show why that intervention
> was allowed.”

Point once across the three numbers: gross, causal, merchant baseline.

## 0:35–1:25 — prove causality, not activity

**Shot:** Click **See the experiment**. Show the arm table and headline KPIs.

> “This is one shared corpus: 500 at-risk orders and 202,005 background payment events.
> Arm A is the fixed retry schedule a merchant already runs. Arm B is rules only. Arm C
> is the full agent. The executor, policy and simulated world are identical; only the
> decision policy changes.
>
> Twenty percent is a never-contacted holdout. It recovered **23.2%** while Recoup did
> absolutely nothing — outages ended, salaries landed, customers retried. Recoup is not
> allowed to claim that money.
>
> The full agent produces **+31.4 percentage points** versus holdout, or **₹10.35 lakh
> incremental**, with a 95% confidence interval shown here. Against the retry schedule
> the merchant already owns, the honest commercial result is **+26.7 points and ₹7.04
> lakh**. That is the number I would sell.”

Pause half a second on the confidence interval. Do not explain bootstrap mechanics.

## 1:25–2:35 — the agent earns its place on one messy payment

**Shot:** Click **Open featured decision trail**. Slowly scroll the drawer from the
diagnosis through the first denial and final recovery.

> “Now one ₹5,720 payment, end to end. The gateway called it `insufficient_funds`, but
> the evidence says `error_source=bank` and the method is an active e-mandate. Recoup
> correctly interprets that as a bounced mandate, not a customer checkout failure.
>
> That distinction changes the action. It proposes mandate re-presentment, computes
> expected value, and sends the proposal to a deterministic policy gate. Every green
> line is a rule that had to pass — approval limit, retry cap, budget, uplift and
> economics — before the executor could act.
>
> After the re-presentments, the next communication falls in quiet hours. The model
> does not get an override. Policy denies it, names the exact rule and defers the
> sequence. At 9 AM the SMS is permitted, the payment recovers, and the sequence stops.
>
> The model proposes. Policy authorizes. The ledger remembers both.”

This is the centre of the demo. Let the red quiet-hours verdict and green final outcome
remain visible long enough to read.

## 2:35–3:15 — show where AI is useful, and where it is not

**Shot:** Close the drawer. Scroll to **Diagnosis accuracy by tier**.

> “I deliberately did not put an LLM everywhere. Detection is a deterministic
> two-proportion test because arithmetic should be reproducible. Known failure shapes
> use lookups. Only 144 of 327 ambiguous records reach the model.
>
> On the noisy corpus, rules-only diagnosis is **69.1%** accurate; the routed agent is
> **99.7%**. On clean data both reach 100%, so the model adds nothing and should not be
> called. The ₹84 model cost is reported separately. AI has to earn its traffic.”

## 3:15–3:55 — failure safety, live in the terminal

**Shot:** Switch to the terminal. Run the two prepared commands.

```bash
.venv/bin/python cli.py trace order_5df8aa203d33da
.venv/bin/python cli.py verify artifacts/ledger_C_AGENT.jsonl
```

> “The same trace is available as data, not just a UI, and the complete ledger verifies
> as a valid hash chain.
>
> I also injected gateway failure up to 60 percent. That run produced 87 transient
> errors, 49 ambiguous timeouts, four breaker trips and 50 dead-lettered actions — with
> **zero double charges**. An ambiguous timeout is reconciled before retry, and three
> identical charge submissions produce one gateway call. Safety is an invariant, not
> a happy-path claim.”

The chaos evidence is already committed in `RESULTS.md` and `artifacts/`; do not spend
a minute running the full chaos sweep during a five-minute video.

## 3:55–4:35 — limits, then the close

**Shot:** Return to the dashboard and use the top nav to jump to **04 guardrails**.

> “The authorization surface is this policy: contact caps, permanent opt-out, DND and
> quiet hours, human approval above ₹75,000, batch budgets, and an economic stop when
> expected value is below 1.5 times action cost.
>
> What is real here: signed Razorpay webhook ingestion, the statistical detector,
> routed diagnosis, policy engine, idempotent executor, holdout measurement and audit
> trail. What is simulated is the customer's eventual payment outcome, so I do not
> pretend the rupee figure is production revenue.
>
> Most agents demonstrate that they can act. Recoup demonstrates when it should not
> act — and proves whether the actions that remain actually made money.”

Stop there. No “thank you” slide and no music.

## Upload and replacement checklist

- Export MP4 at 1080p; keep it under five minutes.
- Watch once at 1× and confirm text is legible and no secret or API key appears.
- Upload to YouTube as **Unlisted** or Google Drive with “Anyone with the link can view.”
- Open the link in an incognito window and play at least 20 seconds.
- Replace the video URL in the submission form. The public GitHub Pages link remains a
  valid fallback until the new recording is ready.

## Likely judge questions

| Question | One-line answer |
|---|---|
| Why deterministic detection? | It is a hypothesis test over counts; non-determinism there would poison downstream measurement. |
| Why no agent framework? | A replayable state machine and explicit policy gate make every transition explainable and testable. |
| Why is lift so high? | The commercial comparison is the smaller +26.7pp versus the merchant's existing retry schedule, and the simulator was deliberately recalibrated after an implausibly strong first run. |
| Where does the model help? | Only on 144 ambiguous records: 69.1% rules-only diagnosis becomes 99.7% with routed model judgment. |
| What breaks first in production? | Sparse detection cells: 1,447 buckets were untestable, and recall falls to 0.10 at 15% outage severity. |
| What is simulated? | Customer outcomes; the integration, decision controls, measurement method and audit machinery are implemented. |
