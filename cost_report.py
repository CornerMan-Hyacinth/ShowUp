"""Usage: python cost_report.py [trace.jsonl] [--price-in USD_PER_M] [--price-out USD_PER_M]
Prints total and average LLM cost per processed message. Free models record $0; pass --price-* to see a
notional cost at any reference rate."""
import argparse
import json

ap = argparse.ArgumentParser()
ap.add_argument("path", nargs="?", default="traces/runs.jsonl")
ap.add_argument("--price-in", type=float)
ap.add_argument("--price-out", type=float)
a = ap.parse_args()

runs = [json.loads(l) for l in open(a.path) if l.strip()]
if not runs:
    raise SystemExit("no runs recorded")
pin, pout, tp, tc, total = a.price_in, a.price_out, 0, 0, 0.0
for r in runs:
    tp, tc = tp + r["prompt_tokens"], tc + r["completion_tokens"]
    total += (r["prompt_tokens"] * pin + r["completion_tokens"] * pout) / 1e6 if pin is not None else r["cost_usd"]
n = len(runs)
label = "notional" if pin is not None else "recorded"
print(f"messages processed : {n}")
print(f"model(s)           : {', '.join(sorted({r['model'] for r in runs}))}")
print(f"tokens in/out      : {tp} / {tc}  (avg {tp // n} / {tc // n} per message)")
print(f"avg LLM calls/msg  : {sum(len(r['generations']) for r in runs) / n:.2f}")
print(f"total cost ({label}) : ${total:.6f}")
print(f"avg cost/message   : ${total / n:.6f}")
