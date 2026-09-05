# Recoup — simple live demo script

This is meant to sound spoken, not read. You do not need to say every word exactly.
Target: **4 to 4½ minutes**.

## Before recording

Run:

```bash
make pitch
```

Open `http://127.0.0.1:8000`. Keep another terminal ready with:

```bash
.venv/bin/python cli.py trace order_5df8aa203d33da
.venv/bin/python cli.py verify artifacts/ledger_C_AGENT.jsonl
```

## 0:00 — introduce yourself and the project

**Show:** The top of the dashboard.

> “Hey, I’m Samyak, and this is Recoup, my project for the AI Revenue Recovery track.
>
> Recoup looks at failed payments, figures out why they failed, and chooses the best
> next step. It might retry later, send a reminder, suggest another payment method, or
> send a high-value case to a person.
>
> The main thing I wanted to solve was knowing whether the agent actually helped. A lot
> of failed payments come back on their own, so a big recovery number can be misleading.”

## 0:35 — show the three numbers

**Show:** Point across the three large cards.

> “For example, the biggest number here is 13.76 lakh rupees. That is the total amount
> recovered in the treated group, and it looks great.
>
> Recoup does not claim all of it. It keeps 20 percent of orders completely untouched,
> which shows how much would return even if the agent did nothing.
>
> After removing that effect, Recoup can claim 10.35 lakh. And compared with the simple
> retry system a merchant may already be using, the extra value is 7.04 lakh. I think
> that is the more honest number.”

## 1:10 — show the experiment

**Do:** Click **See the experiment**.

> “I tested three approaches on the same 500 orders. The first is a basic retry schedule,
> the second uses only rules, and the third is the full Recoup agent. Everything else
> stays the same, so the comparison is fair.
>
> The untouched group recovered 23.2 percent. The full agent reached 54.6 percent,
> which is a 31.4 percentage point improvement. The confidence range is also shown, so
> the result is not just one nice-looking number.
>
> It also reports wasted contacts, opt-outs, and cases it could not safely handle.”

## 1:55 — demo one payment from start to finish

**Do:** Go back to the top and click **Open featured decision trail**. Scroll slowly.

> “Let me show one real decision trail.
>
> This payment is for 5,720 rupees. The raw error says insufficient funds, but the other
> details show a bank-side auto-debit failure. Recoup notices that difference and retries
> the mandate instead of treating it like a normal checkout failure.
>
> The AI only suggests the next action. It cannot directly send a message or move
> money. Every suggestion has to pass the rules first: retry limits, consent, contact
> limits, budget, and whether the action is worth its cost.
>
> Here, the next message would have gone out during quiet hours, so the policy blocks
> it. It waits until 9 AM, allows the SMS, the payment is recovered, and the sequence
> stops. The history includes the action that was refused and why.”

## 2:55 — explain where the AI is actually used

**Do:** Close the trail and move to **Diagnosis accuracy by tier**.

> “I also did not use AI for everything.
>
> Clear errors use normal code. The model only sees messy cases where fields are missing
> or disagree with the written error message.
>
> Out of 327 diagnosed payments, only 144 needed the model. On this noisy data, the
> rules-only version was 69.1 percent accurate, while the full agent reached 99.7
> percent. On clean data both reach 100 percent, so there is no reason to pay for AI
> there.”

## 3:30 — prove the safety work

**Do:** Switch to the terminal and run the two prepared commands.

> “This trail is not just drawn in the dashboard. I can print it from the ledger and
> verify that the full chain has not been changed.
>
> I tested gateway failures as high as 60 percent and submitted the same charge three
> times on purpose. It made one gateway call and produced zero double charges.”

## 4:00 — be honest about what is real, then close

**Do:** Return to the dashboard and click **04 guardrails**.

> “This policy contains quiet hours, DND, opt-outs, contact limits, budgets, human
> approval for large amounts, and a rule to stop when another action is not worth it.
>
> The Razorpay webhook handling, diagnosis, policy checks, executor, holdout experiment
> and audit trail are implemented. The final customer payment outcomes are simulated,
> so the rupee result is not real merchant revenue yet.
>
> It can be connected to real payment data and show not just what it recovered, but what
> it genuinely added and why each action was safe.
>
> That’s Recoup.”

## Recording checklist

- Use your own voice. Small pauses or imperfections will sound more genuine than TTS.
- Record at 1080p and keep the video under five minutes.
- Keep the cursor slow and let the quiet-hours denial remain visible for a moment.
- Do not show `.env`, API keys, notifications, or unrelated browser tabs.
- Save the recording as `/Users/samyak/Desktop/Recoup-Final-Pitch.mov`.

## Short answers if judges ask

| Question | Answer |
|---|---|
| Why not use the model everywhere? | Most payment errors are clear enough for normal code. I use the model only when the data is messy. |
| Why keep an untouched group? | It shows how many payments would recover without Recoup, so I do not claim money the agent did not cause. |
| Can the AI bypass the rules? | No. It only suggests an action; the policy engine makes the final decision. |
| What is simulated? | The customer’s eventual payment result. The pipeline, Razorpay integration, decisions and safety controls are implemented. |
| What was hardest? | Making the evaluation honest. Several early bugs made the results look better than they really were. |
