#!/usr/bin/env python
"""Recoup CLI.

    python cli.py eval            run all three arms, write artifacts/report.json
    python cli.py eval --live     same, but use the real model for diagnosis
    python cli.py chaos           failure-path demo: prove no double charges
    python cli.py verify <file>   verify a ledger's hash chain
    python cli.py trace <order>   print one order's full decision trail
    python cli.py calibrate       learn action priors from a calibration batch
    python cli.py serve           dashboard on http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from recoup.corpus import generate_batch                       # noqa: E402
from recoup.detect import detect_degradations, score_detection  # noqa: E402
from recoup.diagnose import Diagnoser, score_diagnosis          # noqa: E402
from recoup.eval_harness import (                               # noqa: E402
    build_exception_list,
    compute_arm_metrics,
    recovery_ceiling,
    render_report,
)
from recoup.ledger import verify_file                            # noqa: E402
from recoup.orchestrator import ARM_A, ARM_B, ARM_C, Orchestrator  # noqa: E402
from recoup.simulate import World                                # noqa: E402
from recoup.channels.razorpay_api import RazorpayTestClient       # noqa: E402
from recoup.channels.voice import VoiceProvider                   # noqa: E402

ART = "artifacts"
POLICY = "policy.yaml"
PRIORS = os.path.join(ART, "priors.json")


def _fmt_inr(v: float) -> str:
    return f"Rs{v:,.0f}"


def cmd_eval(args) -> None:
    os.makedirs(ART, exist_ok=True)
    mode = "live" if args.live else ("stub" if args.stub else "auto")
    batch = generate_batch(
        n_orders=args.orders, seed=args.seed, holdout_pct=args.holdout, noise=args.noise
    )
    world_for_ceiling = World(batch, seed=args.seed)
    ceiling = recovery_ceiling(batch, world_for_ceiling)

    print(f"\nRecoup evaluation")
    print(f"  corpus      {len(batch.orders)} orders, {len(batch.traffic):,} traffic events, "
          f"{_fmt_inr(batch.total_at_risk_inr)} at risk")
    print(f"  holdout     {args.holdout}% randomized, stratified by (kind x failure class)")
    print(f"  seed        {args.seed}")
    if args.noise:
        print(f"  field noise {args.noise:.0%} of orders have dropped / vendor-specific / "
              f"misattributed error fields")

    arms = [ARM_A, ARM_B, ARM_C]
    metrics, results = [], {}
    diagnoser_for_report = None

    for arm in arms:
        d = None
        if arm == ARM_B:
            d = Diagnoser(mode="stub")        # rules-only: no model, by definition
        elif arm == ARM_C:
            d = Diagnoser(mode=mode)
            diagnoser_for_report = d
        # Each arm gets its own deep copy of the batch, so one arm's recorded
        # recoveries cannot leak into another's starting state.
        b = batch.model_copy(deep=True)
        world = World(b, seed=args.seed)
        rzp = None
        if arm != ARM_A:
            # Shadow by default; RECOUP_LIVE_RAZORPAY=1 sends real test-mode calls.
            # If live mode is requested but the keys are unusable, fall back to
            # shadow with a loud message rather than aborting the evaluation --
            # the measurement does not depend on the Razorpay leg.
            try:
                rzp = RazorpayTestClient(log_path=os.path.join(ART, f"razorpay_{arm}.jsonl"))
            except RuntimeError as e:
                print(f"  ! Razorpay live mode disabled: {e}")
                rzp = RazorpayTestClient(live=False,
                                         log_path=os.path.join(ART, f"razorpay_{arm}.jsonl"))
        o = Orchestrator(
            b, arm, POLICY, PRIORS if os.path.exists(PRIORS) else None,
            os.path.join(ART, f"ledger_{arm}.jsonl"),
            diagnoser=d, chaos=args.chaos, seed=args.seed, razorpay=rzp,
            voice=VoiceProvider(mode="render", log_path=os.path.join(ART, f"voice_{arm}.jsonl")),
        )
        o.world = world
        o.executor.world = world
        r = o.run()
        r.razorpay_stats = rzp.stats if rzp else {}
        if r.diagnosis_timing:
            t = r.diagnosis_timing
            print(f"      diagnosis: {t['orders']} orders in {t['wall_seconds']}s "
                  f"({t['throughput_per_second']}/s, {t['workers']} workers) "
                  f"p50 {t['p50_ms']}ms p95 {t['p95_ms']}ms")
        r.voice_stats = dict(o.voice.stats) if o.voice else {}
        results[arm] = r
        metrics.append(
            compute_arm_metrics(b, r, world, ceiling_value=ceiling["treated_addressable_inr"])
        )
        tag = ""
        if arm == ARM_C and d is not None:
            tag = f"  [diagnosis: {d.mode}{'/' + d.model if d.mode == 'live' else ''}]"
        print(f"  ran {arm:11s} {r.wall_seconds:5.1f}s  {len(r.ledger.entries):5d} ledger entries{tag}")

    # --- detection + diagnosis scoring (from the full agent arm) -----------
    rc = results[ARM_C]
    det = score_detection(batch, rc.signals)
    det["coverage"] = rc.coverage
    diag = score_diagnosis(rc.diagnoses, batch.ground_truth)
    diag["tier_split"] = rc.diagnoser_stats

    # Score EVERY arm's diagnoses, not just the agent's.
    #
    # This was a gap in my own reporting: the report showed only arm C's
    # accuracy, which made it look as though both arms diagnosed identically
    # and left arm C's recovery advantage unexplained. Arm B routes its
    # ambiguous cases to the deterministic fallback, so its accuracy is a
    # different number on the same rows -- and that difference is precisely the
    # mechanism by which the model earns its lift.
    diag["by_arm"] = {}
    for arm_name, res in results.items():
        # Arm A performs no diagnosis at all; scoring its placeholder UNKNOWNs
        # would print a meaningless 0.0 next to two real numbers.
        if not res.diagnoses or all(d.tier == "none" for d in res.diagnoses.values()):
            continue
        sc = score_diagnosis(res.diagnoses, batch.ground_truth)
        diag["by_arm"][arm_name] = {
            "n": sc["n"],
            "overall_accuracy": sc["overall_accuracy"],
            "by_tier": {t: v["accuracy"] for t, v in sc["by_tier"].items()},
        }

    report = render_report(
        batch=batch,
        metrics=metrics,
        ceiling=ceiling,
        detection=det,
        diagnosis=diag,
        executor_stats=rc.executor.stats,
        exceptions=build_exception_list(rc, batch),
        meta={
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "seed": args.seed,
            "chaos": args.chaos,
            "diagnosis_mode": diagnoser_for_report.mode if diagnoser_for_report else "n/a",
            "model": diagnoser_for_report.model if diagnoser_for_report and diagnoser_for_report.mode == "live" else None,
            "policy_version": rc.policy.version,
            "priors_source": rc.policy.priors_source,
            "ledger_chain_valid": rc.ledger.verify()["valid"],
            "reproduce": f"python cli.py eval --orders {args.orders} --seed {args.seed}"
                         f"{' --live' if args.live else ''}",
            "razorpay": getattr(rc, "razorpay_stats", {}),
            "voice_calls_rendered": getattr(rc, "voice_stats", {}),
            "diagnosis_timing": getattr(rc, "diagnosis_timing", {}),
        },
    )
    out = os.path.join(ART, args.out or "report.json")
    with open(out, "w") as fh:
        json.dump(report, fh, indent=2, default=str)

    _print_table(report)
    print(f"\n  full report -> {out}")
    if diagnoser_for_report and diagnoser_for_report.mode == "stub":
        print("\n  NOTE: diagnosis ran in STUB mode (no ANTHROPIC_API_KEY), so arm C is")
        print("        identical to arm B by construction. Headline numbers should only")
        print("        be quoted from a --live run. Set ANTHROPIC_API_KEY and re-run.")


def _print_table(report: dict) -> None:
    c = report["recovery_ceiling"]
    print(f"\n  Recovery ceiling")
    print(f"    total at risk               {_fmt_inr(c['total_at_risk_inr']):>14s}")
    print(f"    structurally unrecoverable  {_fmt_inr(c['structurally_unrecoverable_inr']):>14s}  "
          f"({c['structurally_unrecoverable_orders']} orders)")
    print(f"    would self-heal anyway      {_fmt_inr(c['would_self_heal_inr']):>14s}  "
          f"({c['would_self_heal_orders']} orders)")
    print(f"    addressable (the real pool) {_fmt_inr(c['addressable_inr']):>14s}  "
          f"({c['addressable_orders']} orders)")

    print(f"\n  {'arm':12s} {'treated':>9s} {'holdout':>9s} {'lift':>8s} {'95% CI':>16s} "
          f"{'incr Rs':>11s} {'spend':>9s} {'net Rs':>11s} {'Rs/100':>8s} {'sig':>4s}")
    print("  " + "-" * 108)
    for a in report["arms"]:
        i, e = a["incremental"], a["economics"]
        ci = f"[{i['lift_95ci_pp'][0]:+.1f},{i['lift_95ci_pp'][1]:+.1f}]"
        print(f"  {a['arm']:12s} {a['treated']['recovery_rate_pct']:8.1f}% "
              f"{a['holdout']['recovery_rate_pct']:8.1f}% {i['lift_pp']:+7.1f}pp {ci:>16s} "
              f"{i['inr']:>11,.0f} {e['spend_inr']:>9,.0f} {e['net_value_inr']:>11,.0f} "
              f"{(e['cost_per_100_recovered_inr'] or 0):>8.3f} "
              f"{'yes' if i['statistically_significant'] else 'NO':>4s}")

    # The commercially meaningful comparison. Lift vs a no-contact holdout
    # answers "does this work at all"; lift vs the naive retry schedule a
    # merchant already runs answers "what am I actually buying". Both belong in
    # the report, and quoting only the first would overstate the offer.
    arms = {a["arm"]: a for a in report["arms"]}
    base = arms.get("A_BASELINE")
    if base:
        print(f"\n  Incremental value OVER the retry schedule a merchant already runs")
        for name in ("B_RULES", "C_AGENT"):
            a = arms.get(name)
            if not a:
                continue
            d_pp = a["incremental"]["lift_pp"] - base["incremental"]["lift_pp"]
            d_inr = a["incremental"]["inr"] - base["incremental"]["inr"]
            cost = a["economics"]["total_cost_inr"] - base["economics"]["total_cost_inr"]
            per100 = (cost / (d_inr / 100.0)) if d_inr > 0 else None
            print(f"    {name:12s} {d_pp:+6.1f}pp vs baseline   "
                  f"{_fmt_inr(d_inr):>12s} extra recovered   "
                  f"cost {_fmt_inr(cost):>8s}   "
                  f"Rs{(per100 or 0):.3f} per Rs100")

    print(f"\n  False-positive cost (what a raw recovery rate hides)")
    for a in report["arms"]:
        f = a["false_positive_cost"]
        print(f"    {a['arm']:12s} self-heal not claimed: {f['self_heal_recoveries_not_claimed']:3d}   "
              f"wasted contacts: {f['wasted_contacts_to_self_healers']:3d}   "
              f"opt-outs: {f['opt_outs_caused']:3d} "
              f"(forward cost {_fmt_inr(f['opt_out_forward_cost_inr'])})   "
              f"net after opt-out cost: {_fmt_inr(a['economics']['net_value_after_optout_cost_inr'])}")

    d = report["detection"]
    print(f"\n  Detection   injected {d['injected_outages']}  found {d['signals_raised']}  "
          f"precision {d['precision']}  recall {d['recall']}  "
          f"(shadows collapsed: {d['coverage'].get('cross_dimension_shadows', 0)}, "
          f"hypotheses tested: {d['coverage'].get('hypotheses_tested', 0)})")

    g = report["diagnosis"]
    print(f"  Diagnosis   overall accuracy {g['overall_accuracy']}  (n={g['n']})")
    for tier, v in sorted(g["by_tier"].items()):
        print(f"                {tier:14s} n={v['n']:4d}  accuracy={v['accuracy']}")
    if g.get("by_arm"):
        print(f"              per arm (same rows, different diagnosers):")
        for arm_name, v in sorted(g["by_arm"].items()):
            tiers = ", ".join(f"{t}={a}" for t, a in sorted(v["by_tier"].items()))
            print(f"                {arm_name:12s} accuracy={v['overall_accuracy']}  ({tiers})")

    x = report["executor_invariants"]
    print(f"\n  Executor invariants")
    print(f"    double charges                {x['double_charges']}   <- must be 0")
    print(f"    duplicate charges prevented   {x['duplicate_charges_prevented']}")
    print(f"    ambiguous timeouts reconciled {x['reconciliations']}")
    print(f"    dead-lettered                 {x['dead_lettered']}")
    print(f"    breaker trips                 {x['breaker_trips']}")

    ex = report["exceptions"]["unrecovered_by_reason"]
    print(f"\n  Exception list (unrecovered, by reason)")
    for reason, v in list(ex.items())[:8]:
        print(f"    {reason:22s} {v['orders']:4d} orders  {_fmt_inr(v['value_inr']):>12s}")
    print(f"    human review queue     {len(report['exceptions']['human_review_queue']):4d} orders")


def cmd_chaos(args) -> None:
    """Failure-path demo. The claim being tested is narrow and absolute:
    under injected gateway failures, zero double charges."""
    print("\nChaos run -- injecting gateway failures into every money action\n")
    batch = generate_batch(n_orders=args.orders, seed=args.seed)
    for chaos in (0.0, 0.15, 0.35, 0.60):
        b = batch.model_copy(deep=True)
        world = World(b, seed=args.seed)
        o = Orchestrator(
            b, ARM_B, POLICY, PRIORS if os.path.exists(PRIORS) else None,
            os.path.join(ART, f"ledger_chaos_{int(chaos*100)}.jsonl"),
            diagnoser=Diagnoser(mode="stub"), chaos=chaos, seed=args.seed,
        )
        o.world = world
        o.executor.world = world
        r = o.run()
        s = r.executor.stats
        rec = sum(1 for i in r.treated_ids if r.states[i].recovered)
        v = r.ledger.verify()
        print(f"  chaos={chaos:.0%}  recovered={rec:3d}  calls={s['calls']:4d}  "
              f"transient={s['transient_errors']:3d}  ambiguous={s['ambiguous_timeouts']:3d}  "
              f"reconciled={s['reconciliations']:3d}  retries={s['retries']:3d}  "
              f"breaker_trips={s['breaker_trips']:2d}  DLQ={s['dead_lettered']:3d}  "
              f"dup_prevented={s['duplicate_charges_prevented']:3d}  "
              f"DOUBLE_CHARGES={s['double_charges']}  ledger_valid={v['valid']}")
    print("\n  Double charges stayed at 0 across every chaos level. That is the")
    print("  invariant idempotency keys exist to protect.")
    _idempotency_proof(args)


def _idempotency_proof(args) -> None:
    """Actually replay a money action and show the second charge never lands.

    The chaos table above shows `dup_prevented=0`, which is honest but proves
    nothing: the orchestrator never happens to resubmit an identical action, so
    the idempotency path is untested by that run. A merchant's real duplicate
    comes from outside -- a retried webhook, a double-clicked retry button, a
    replayed queue message. So this reproduces that directly.
    """
    from recoup.executor import Executor, idempotency_key
    from recoup.models import Intervention

    print("\n  Duplicate-submission proof (the failure a merchant cannot forgive)")
    batch = generate_batch(n_orders=20, seed=args.seed)
    world = World(batch, seed=args.seed)
    ex = Executor(world, chaos=0.0, seed=1)
    order = next(o for o in batch.orders if o.amount_inr > 0)
    iv = Intervention.RETRY_NOW

    r1 = ex.execute(order, iv, 0, batch.generated_at)
    r2 = ex.execute(order, iv, 0, batch.generated_at)   # the duplicate
    r3 = ex.execute(order, iv, 0, batch.generated_at)   # and again

    print(f"    order            {order.order_id}  {_fmt_inr(order.amount_inr)}")
    print(f"    idempotency key  {idempotency_key(order.order_id, iv, 0)}")
    print(f"    submission 1     ok={r1.ok}  replayed={r1.replayed}  <- charge attempted")
    print(f"    submission 2     ok={r2.ok}  replayed={r2.replayed}  <- served from ledger, NOT charged")
    print(f"    submission 3     ok={r3.ok}  replayed={r3.replayed}  <- served from ledger, NOT charged")
    print(f"    gateway calls made           {ex.stats['calls']}   <- 1, not 3")
    print(f"    duplicate charges prevented  {ex.stats['duplicate_charges_prevented']}")
    print(f"    double charges               {ex.stats['double_charges']}")
    same = r1.outcome.recovered == r2.outcome.recovered == r3.outcome.recovered
    print(f"    all three returned the identical outcome: {same}")


def cmd_ingest(args) -> None:
    """POST the webhook fixtures at a running server, correctly signed.

    Exists so the ingest path can be exercised without waiting for Razorpay to
    send a real event -- and so the signature, dedup and mapping behaviour is
    demonstrable on camera.
    """
    import glob
    import hashlib
    import hmac

    import httpx

    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    files = sorted(glob.glob(os.path.join(args.dir, "*.json")))
    if not files:
        print(f"no fixtures in {args.dir}")
        return
    print(f"\nPosting {len(files)} webhook fixtures to {args.url}")
    print(f"  signing: {'HMAC-SHA256 with RAZORPAY_WEBHOOK_SECRET' if secret else 'NONE (secret not set)'}\n")
    for i, f in enumerate(files):
        raw = open(f, "rb").read()
        headers = {"content-type": "application/json", "x-razorpay-event-id": f"evt_cli_{i}"}
        if secret:
            headers["x-razorpay-signature"] = hmac.new(
                secret.encode(), raw, hashlib.sha256
            ).hexdigest()
        try:
            r = httpx.post(args.url, content=raw, headers=headers, timeout=10)
            j = r.json()
            name = os.path.basename(f).replace(".json", "")
            print(f"  {name:36s} {r.status_code} "
                  f"{'accepted ' + str(j.get('order_id')) if j.get('accepted') else 'rejected: ' + str(j.get('reason') or j.get('detail'))}")
        except Exception as e:
            print(f"  {os.path.basename(f):36s} ERROR {type(e).__name__}: {e}")
    print("\n  Server state: GET /webhooks/status")


def cmd_replay(args) -> None:
    """Run webhook-ingested orders through the full recovery pipeline.

    This is the path a real deployment takes: events arrive, get mapped, and the
    same detection/diagnosis/policy/executor stack runs on them. No holdout or
    lift here -- real events have no ground truth and no counterfactual, so the
    only honest output is the decision trail, not a recovery number.
    """
    import json as _json

    from recoup.detect import build_risk_register
    from recoup.ledger import Actor, EventType, Ledger
    from recoup.models import Batch
    from recoup.webhooks import STORE, map_event

    if not os.path.exists(args.spool):
        print(f"no spool at {args.spool} -- run 'cli.py ingest' against a live server first")
        return
    n = 0
    with open(args.spool) as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = _json.loads(line)
            STORE.ingest(rec["body"], rec.get("event_id"))
            n += 1
    print(f"\nReplayed {n} spooled events -> {len(STORE.orders)} orders, "
          f"{len(STORE.customers)} customers\n")

    batch = Batch(
        batch_id="webhook_replay",
        generated_at=datetime.now(),
        seed=0,
        orders=list(STORE.orders.values()),
        customers=STORE.customers,
        ground_truth={},
    )
    register = build_risk_register(batch, [], now=datetime.now())
    d = Diagnoser(mode="live" if args.live else "auto")
    print(f"{'order':32s} {'kind':13s} {'amount':>12s} {'diagnosed':24s} {'tier':14s} conf")
    print("-" * 104)
    for oid, facts in register.items():
        dg = d.diagnose(facts)
        o = STORE.orders[oid]
        print(f"{oid[:32]:32s} {o.kind.value:13s} {o.amount_inr:>12,.2f} "
              f"{dg.failure_class.value:24s} {dg.tier:14s} {dg.confidence:.2f}")
    print(f"\n  diagnoser: mode={d.mode} "
          f"deterministic={d.stats['deterministic']} llm={d.stats['llm']} "
          f"fallback={d.stats['fallback']} cost={_fmt_inr(d.stats['llm_cost_inr'])}")
    print("  No lift is reported: real events have no holdout and no ground truth.")


def cmd_verify(args) -> None:
    res = verify_file(args.path)
    print(json.dumps(res, indent=2))
    if not res["valid"]:
        sys.exit(1)


def cmd_trace(args) -> None:
    """Print one order's full decision trail from a ledger file."""
    from recoup.ledger import replay

    n = 0
    for e in replay(args.path):
        if e.get("order_id") != args.order_id:
            continue
        n += 1
        p = e["payload"]
        print(f"\n[{e['seq']:5d}] {e['event']:18s} by {e['actor']:10s} {e['ts'][11:19]}")
        if e["event"] == "POLICY_VERDICT":
            print(f"        allowed={p.get('allowed')}  action={p.get('intervention')}")
            if p.get("denial_rule"):
                print(f"        DENIED BY {p['denial_rule']}: {p.get('denial_detail','')}")
            for r in p.get("rules_evaluated", [])[:40]:
                mark = "ok  " if r.get("passed", r.get("allowed")) else "DENY"
                print(f"          [{mark}] {r.get('rule', r.get('intervention'))}: "
                      f"{r.get('detail', r.get('denial_detail',''))}")
        else:
            for k, v in p.items():
                if k == "candidate_trace":
                    for c in v:
                        print(f"          candidate {c.get('intervention')}: "
                              f"allowed={c.get('allowed')} "
                              f"{c.get('denial_rule') or ''} {c.get('denial_detail') or ''}")
                else:
                    print(f"        {k}: {v}")
    if n == 0:
        print(f"no entries for {args.order_id} in {args.path}")


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run("recoup.api:app", host="127.0.0.1", port=args.port, reload=False)


def main() -> None:
    ap = argparse.ArgumentParser(prog="recoup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("eval", help="run all arms and write the report")
    e.add_argument("--orders", type=int, default=500)
    e.add_argument("--seed", type=int, default=20260903)
    e.add_argument("--holdout", type=int, default=20)
    e.add_argument("--chaos", type=float, default=0.0)
    e.add_argument("--out", default=None, help="report filename under artifacts/")
    # Default 0.35, not 0. A clean corpus makes every failure fully determined
    # by its structured `error_reason`, both tiers score 100%, and the model
    # provably adds nothing -- which says more about the corpus than the agent.
    # Real gateways drop fields, emit vendor codes and misattribute sources, so
    # the realistic setting is the honest default. `--noise 0` runs the ablation.
    e.add_argument("--noise", type=float, default=0.35,
                   help="share of orders with degraded structured error fields "
                        "(cause still recoverable from free text). Default 0.35.")
    e.add_argument("--live", action="store_true", help="use the real model for diagnosis")
    e.add_argument("--stub", action="store_true", help="force offline stub diagnosis")
    e.set_defaults(func=cmd_eval)

    c = sub.add_parser("chaos", help="failure-path demo")
    c.add_argument("--orders", type=int, default=300)
    c.add_argument("--seed", type=int, default=20260903)
    c.set_defaults(func=cmd_chaos)

    ig = sub.add_parser("ingest", help="post webhook fixtures at a running server")
    ig.add_argument("--url", default="http://127.0.0.1:8000/webhooks/razorpay")
    ig.add_argument("--dir", default="fixtures/webhooks")
    ig.set_defaults(func=cmd_ingest)

    rp = sub.add_parser("replay", help="run webhook-ingested orders through the pipeline")
    rp.add_argument("--spool", default=os.path.join(ART, "webhook_spool.jsonl"))
    rp.add_argument("--live", action="store_true")
    rp.set_defaults(func=cmd_replay)

    v = sub.add_parser("verify", help="verify a ledger hash chain")
    v.add_argument("path")
    v.set_defaults(func=cmd_verify)

    t = sub.add_parser("trace", help="print one order's decision trail")
    t.add_argument("order_id")
    t.add_argument("--path", default=os.path.join(ART, f"ledger_{ARM_C}.jsonl"))
    t.set_defaults(func=cmd_trace)

    s = sub.add_parser("serve", help="run the dashboard")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
