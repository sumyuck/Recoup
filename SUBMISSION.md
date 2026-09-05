# Submission form answers

These are written to be pasted directly into the two long-answer fields.

## Project Objectives — What does it solve?

> I built Recoup to help businesses recover failed payments without blindly retrying or
> repeatedly messaging every customer.
>
> It looks at the reason behind each failed payment and chooses the most suitable next
> step. For example, it can wait and retry a temporary bank failure, send a payment-link
> reminder, ask the customer to update an expired card, or move a large and sensitive
> case to a human.
>
> The part I cared about most was measuring whether the system actually helped. Many
> failed payments recover on their own, so a tool can easily take credit for money it did
> not recover. Recoup keeps 20% of orders untouched and compares its results against that
> group. In my 500-order evaluation, the full agent improved recovery by 31.4 percentage
> points over the untouched group. Compared with a basic retry schedule that a merchant
> may already use, it added 26.7 points, or about ₹7.04 lakh in the simulation.
>
> The AI is also kept within clear limits. It can suggest an action, but a normal policy
> engine checks consent, DND, quiet hours, contact limits, budgets and approval rules
> before anything happens. Every decision is saved in an audit trail, including actions
> that were blocked and the reason they were blocked.

## Build Challenges & Technical Obstacles

> The hardest part was not getting the agent to take actions. It was making sure the
> results were honest and the actions were safe.
>
> My first version detected five incidents even though I had only created two outages.
> After checking the data, I found that the same bank outage was appearing in several
> different payment slices. I changed the detector so it groups related signals under one
> main incident instead of counting the same problem multiple times. I also corrected for
> the thousands of statistical checks being run, because otherwise random noise looked
> like a real outage.
>
> I also found a bug that made the project look better than it was. Payments that would
> recover on their own were being marked as recovered before the agent could contact
> them. Because of that, the report showed zero wasted messages. I changed the simulator
> so natural recovery happens over time, just like it would in a real system. The final
> recovery number became lower, but much more believable.
>
> Another issue was that I treated every blocked action as final. A message blocked at
> night was being abandoned instead of retried after quiet hours. I separated temporary
> blocks, permanent blocks and cases that need human review. Now the system can wait and
> continue later without bypassing the policy.
>
> Finally, the model was initially worse than simple rules because my prompt gave it the
> wrong clues for a few Razorpay error types. I fixed the mappings, added messy and
> incomplete gateway data to the tests, and limited the model to cases where normal rules
> are not enough. That made its value measurable instead of using AI just for the sake of
> it.

## Links and final fields

| Field | Value |
|---|---|
| Selected Track | Track 3: AI Revenue Recovery |
| Project Name | Recoup — AI revenue recovery that measures what it actually adds |
| GitHub Repository | https://github.com/sumyuck/Recoup |
| Current fallback video | https://sumyuck.github.io/Recoup/ |

Before submitting, confirm that the final video plays while signed out and is under five
minutes. The form cannot be edited after submission.
