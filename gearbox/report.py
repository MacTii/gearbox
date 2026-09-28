"""Summarize logs/decisions.jsonl: tier distribution, decision sources, fallbacks,
and the prompts worth reviewing when tuning heuristics."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .config import TIER_ORDER, load_config, request_cost


def print_cost_savings(decisions: list[dict], config_arg: str | None) -> None:
    """Actual spend at the served tier vs. the hypothetical spend had every request gone to the
    top tier (what Claude Code would have used without gearbox). Based on real input/output token
    counts parsed from the API responses (prompt-cache reads/writes included) - approximate: a real
    top-tier session would have had a different cache hit pattern."""
    try:
        cfg = load_config(config_arg)
    except (OSError, ValueError):
        return
    pricing = cfg.pricing
    top = next((t for t in reversed(TIER_ORDER) if t in cfg.intercept_models), TIER_ORDER[-1])
    if not pricing or top not in pricing:
        return

    priced = [r for r in decisions if r.get("tier") in pricing and request_cost(r, pricing[top]) is not None]
    if not priced:
        return

    actual = sum(request_cost(r, pricing[r["tier"]]) for r in priced)
    baseline = sum(request_cost(r, pricing[top]) for r in priced)
    saved = baseline - actual
    pct = (saved / baseline * 100) if baseline else 0
    n_all = len(decisions)
    print(f"\nestimated cost ({len(priced)}/{n_all} decisions had usage data, {top} pricing as baseline):")
    print(f"  actual:   ${actual:.4f}")
    print(f"  baseline: ${baseline:.4f}  (if every request had gone to {top})")
    print(f"  saved:    ${saved:.4f}  ({pct:.0f}%)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--show", type=int, default=15, help="how many prompts to list per section")
    ap.add_argument("--config", help="config to read pricing/tiers from (default: same resolution as serve)")
    args = ap.parse_args(argv)

    path = Path(args.log)
    if not path.exists():
        print(f"no decisions logged yet ({path})")
        return 0
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    decisions = [r for r in rows if "classified" in r]
    if not decisions:
        print("no decisions logged yet")
        return 0

    print(f"decisions: {len(decisions)}\n")
    for title, key in [("final tier", "tier"), ("classified", "classified"), ("source", "source"),
                       ("served by", "served_model")]:
        c = Counter(r.get(key) for r in decisions)
        print(f"{title:>11}: " + ", ".join(f"{k}={v} ({v / len(decisions):.0%})" for k, v in c.most_common()))

    fallbacks = [r for r in rows if r.get("fallback")]
    print(f"\n400 fallbacks: {len(fallbacks)}")
    for r in fallbacks[: args.show]:
        print(f"  {r['fallback']['from']}: {r['fallback']['error'][:160]}")

    lat = sorted(r["route_ms"] for r in decisions if "route_ms" in r)
    if lat:
        print(f"\nrouting latency ms: p50={lat[len(lat) // 2]} p95={lat[int(len(lat) * 0.95)]} max={lat[-1]}")

    print_cost_savings(decisions, args.config)

    # Judge calls = heuristics were unsure. Reviewing these is how you find new keywords.
    judged = [r for r in decisions if r.get("source") == "judge"]
    print(f"\n--- decided by judge ({len(judged)}), newest first ---")
    for r in judged[::-1][: args.show]:
        print(f"  [{r['classified']:>6}] score={r['score']:+.1f}  {r['prompt'][:110]!r}")

    held = [r for r in decisions if r.get("tier") != r.get("classified")]
    print(f"\n--- policy/constraint kept a higher tier than classified ({len(held)}) ---")
    for r in held[::-1][: args.show]:
        why = r.get("constraints") or f"policy (previous={r.get('previous')})"
        print(f"  {r['classified']} -> {r['tier']}  {why}  {r['prompt'][:80]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
