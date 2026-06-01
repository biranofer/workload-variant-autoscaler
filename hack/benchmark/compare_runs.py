#!/usr/bin/env python3
"""Compare two or more two-variant benchmark runs side by side.

Loads each run's `results.json` and `metrics/processed/replica_status_timeseries.json`,
computes:
  - request totals (successful / errored / incomplete) and SLO metrics
    (mean / median TTFT, mean E2E latency, mean TPOT)
  - per-variant pod-seconds via trapezoid-rule integration of ready replicas
  - GPU-minutes and cost-weighted total
    (cost = primaryCost · primary_pod_seconds + v2Cost · v2_pod_seconds)
  - cost per successful request

Prints a side-by-side table and (optionally) writes a JSON summary.

Examples
--------
  # Two runs (auto-labels from dir names)
  python hack/benchmark/compare_runs.py \
      biran-20260601-163807-046/results/guidellm-1780321137-yr59g7_1 \
      biran-20260601-175641-667/results/guidellm-1780325839-efsct5_1

  # With explicit labels (one --label per run) and a different cost weighting
  python hack/benchmark/compare_runs.py \
      --label "WVA" --label "HPA-EPP" \
      --cost-primary 10 --cost-v2 5 \
      <run-A> <run-B>

  # Save the structured summary as JSON
  python hack/benchmark/compare_runs.py --json out/comparison.json <run-A> <run-B>
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def _safe_get(d, *keys, default=None):
    for k in keys:
        if d is None:
            return default
        d = d.get(k) if isinstance(d, dict) else None
    return default if d is None else d


def load_run(results_dir, primary_cost, v2_cost):
    rd = Path(results_dir).resolve()
    with (rd / "results.json").open() as f:
        m = json.load(f)["benchmarks"][0]["metrics"]
    with (rd / "metrics" / "processed" / "replica_status_timeseries.json").open() as f:
        snaps = json.load(f)["snapshots"]

    pts = []
    for s in snaps:
        ts = datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")).timestamp()
        p = v = 0
        for c in s["controllers"]:
            r = c.get("ready_replicas") or 0
            if c["name"].endswith("-v2"):
                v = r
            else:
                p = r
        pts.append((ts, p, v))

    primary_ps = v2_ps = 0.0
    for i in range(len(pts) - 1):
        dt = pts[i + 1][0] - pts[i][0]
        primary_ps += dt * (pts[i][1] + pts[i + 1][1]) / 2
        v2_ps += dt * (pts[i][2] + pts[i + 1][2]) / 2

    cost = primary_cost * primary_ps + v2_cost * v2_ps
    succ = m["request_totals"]["successful"]
    tot = m["request_totals"]["total"]
    err = m["request_totals"]["errored"]
    inc = m["request_totals"]["incomplete"]

    return {
        "results_dir": str(rd),
        "label": rd.parts[-3] if len(rd.parts) >= 3 else rd.name,
        "successful": succ,
        "total": tot,
        "errored": err,
        "incomplete": inc,
        "success_pct": (100.0 * succ / tot) if tot else 0.0,
        "ttft_mean_ms":  _safe_get(m, "time_to_first_token_ms", "successful", "mean"),
        "ttft_median_ms": _safe_get(m, "time_to_first_token_ms", "successful", "median"),
        "latency_mean_s": _safe_get(m, "request_latency", "successful", "mean"),
        "tpot_mean_ms":  _safe_get(m, "time_per_output_token_ms", "successful", "mean"),
        "primary_peak":  max((p for _, p, _ in pts), default=0),
        "v2_peak":       max((v for _, _, v in pts), default=0),
        "primary_pod_seconds": primary_ps,
        "v2_pod_seconds":      v2_ps,
        "gpu_minutes":         (primary_ps + v2_ps) / 60.0,
        "cost_weighted":       cost,
        "cost_per_successful": (cost / succ) if succ else float("inf"),
        "cost_weights":        {"primary": primary_cost, "v2": v2_cost},
    }


def render_table(rows):
    cols = ["metric"] + [r["label"] for r in rows]
    width = max(20, max(len(c) for c in cols))

    def line(label, key, fmt=".0f"):
        cells = [label]
        for r in rows:
            v = r.get(key)
            cells.append(f"{v:.{fmt[1:]}}" if isinstance(v, float)
                         else (str(v) if v is not None else "-"))
        print(" | ".join(f"{c:>{width}}" for c in cells))

    print(" | ".join(f"{c:>{width}}" for c in cols))
    print("-" * (width * len(cols) + 3 * (len(cols) - 1)))

    line("Successful (of total)", "successful")
    line("Errored",               "errored")
    line("Incomplete",            "incomplete")
    line("Success %",             "success_pct", ".1f")
    print("-" * (width * len(cols) + 3 * (len(cols) - 1)))
    line("Mean TTFT (ms)",        "ttft_mean_ms")
    line("Median TTFT (ms)",      "ttft_median_ms")
    line("Mean E2E latency (s)",  "latency_mean_s", ".1f")
    line("Mean TPOT (ms)",        "tpot_mean_ms",   ".1f")
    print("-" * (width * len(cols) + 3 * (len(cols) - 1)))
    line("Primary peak replicas", "primary_peak")
    line("v2 peak replicas",      "v2_peak")
    line("Primary pod-seconds",   "primary_pod_seconds")
    line("v2 pod-seconds",        "v2_pod_seconds")
    line("GPU-minutes",           "gpu_minutes",   ".2f")
    weights = rows[0]["cost_weights"]
    line(f"COST ({weights['primary']}·pri + {weights['v2']}·v2)", "cost_weighted")
    line("Cost per successful",   "cost_per_successful", ".2f")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results_dirs", nargs="+",
                    help="Two or more results dirs (e.g. biran-…/results/guidellm-…_1)")
    ap.add_argument("--label", action="append", dest="labels", default=[],
                    help="Label for one run; repeat once per results dir. "
                         "If omitted, the parent run dir name is used.")
    ap.add_argument("--cost-primary", type=float, default=10.0,
                    help="Cost weight for primary variant (default 10)")
    ap.add_argument("--cost-v2", type=float, default=5.0,
                    help="Cost weight for v2 variant (default 5)")
    ap.add_argument("--json", metavar="PATH",
                    help="Also write a JSON summary to this path")
    args = ap.parse_args()

    if len(args.results_dirs) < 2:
        ap.error("at least two results dirs are required")
    if args.labels and len(args.labels) != len(args.results_dirs):
        ap.error(f"--labels expects {len(args.results_dirs)} values, got {len(args.labels)}")

    rows = []
    for i, rd in enumerate(args.results_dirs):
        try:
            row = load_run(rd, args.cost_primary, args.cost_v2)
        except Exception as e:
            print(f"ERROR loading {rd}: {e}", file=sys.stderr)
            sys.exit(2)
        if args.labels:
            row["label"] = args.labels[i]
        rows.append(row)

    render_table(rows)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"runs": rows}, indent=2))
        print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
