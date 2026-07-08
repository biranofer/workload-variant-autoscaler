#!/usr/bin/env python3
"""
Post-process llm-d-benchmark results into a markdown table.

Produces the exact table format used in docs/benchmark.md.

Usage:
    # Single run:
    python hack/benchmark/postprocess.py results/guidellm-*_1

    # Three runs (Run 1 | Run 2 | Run 3 | Avg):
    python hack/benchmark/postprocess.py results/guidellm-*_1 \\
                                          results/guidellm-*_2 \\
                                          results/guidellm-*_3

    # With scenario header:
    python hack/benchmark/postprocess.py --scenario "Prefill Heavy — Qwen/Qwen3-32B (600s)" \\
        results/guidellm-*_1 results/guidellm-*_2 results/guidellm-*_3

    # Two-variant run (primary TP=2, secondary TP=1 with suffix "v2"):
    python hack/benchmark/postprocess.py --secondary-suffix v2 \\
        --gpus-per-primary 2 --gpus-per-secondary 1 \\
        results/guidellm-*_1

    # V1 vs V2 analyzer comparison (single-variant, with GPU time):
    python hack/benchmark/postprocess.py --labels "V1 Analyzer" "V2 Analyzer" \\
        --gpus-per-replica 2 \\
        results/inference-perf-*_v1 results/inference-perf-*_v2
"""

import argparse
import json
import os
import re
import sys
from statistics import mean

METRICS = [
    "Avg TTFT (ms)",
    "P50 TTFT (ms)",
    "P95 TTFT (ms)",
    "P99 TTFT (ms)",
    "Avg TPOT (ms/token)",
    "P50 TPOT (ms/token)",
    "P95 TPOT (ms/token)",
    "P99 TPOT (ms/token)",
    "Avg replicas",
    "Max replicas",
    "GPU time (GPU·min)",
    "Avg KV cache utilization",
    "Avg queue depth (EPP)",
    "Error count",
    "Avg pod startup (s)",
]

# Metrics list used for two-variant runs; replica rows are split per variant
# and a weighted-cost row replaces the plain replica rows.
def _variant_metrics(primary_label, secondary_label):
    return [
        "Avg TTFT (ms)",
        "P50 TTFT (ms)",
        "P95 TTFT (ms)",
        "P99 TTFT (ms)",
        "Avg TPOT (ms/token)",
        "P50 TPOT (ms/token)",
        "P95 TPOT (ms/token)",
        "P99 TPOT (ms/token)",
        f"Avg {primary_label} replicas",
        f"Max {primary_label} replicas",
        f"Avg {secondary_label} replicas",
        f"Max {secondary_label} replicas",
        "Avg KV cache utilization",
        "Avg queue depth (EPP)",
        "Error count",
        "Avg pod startup (s)",
        "Cost (weighted avg replicas × GPU/hr)",
    ]


def _read_tensor_from_yaml(yaml_path):
    """Return the first `tensor: N` value found in a YAML file, or 1 if absent."""
    if not yaml_path or not os.path.isfile(yaml_path):
        return 1
    with open(yaml_path) as f:
        for line in f:
            m = re.match(r'\s*tensor:\s*(\d+)', line)
            if m:
                return int(m.group(1))
    return 1


def _parse_prometheus_value(line, metric_name):
    """Extract a float from a Prometheus exposition-format line."""
    if not line.startswith(metric_name):
        return None
    rest = line[len(metric_name):]
    if rest and rest[0] not in ("{", " "):
        return None
    if rest.startswith("{"):
        close = rest.find("}")
        if close < 0:
            return None
        rest = rest[close + 1:]
    try:
        return float(rest.strip())
    except (ValueError, IndexError):
        return None


def _extract_latency(results_dir):
    """Avg/P50/P95/P99 TTFT and TPOT.

    Supports two harness result formats:
      - GuideLLM: results.json with benchmarks[0].metrics.<key>_ms.successful.{mean,percentiles}
      - inference-perf: summary_lifecycle_metrics.json with successes.latency.<key>.{mean,p50,...} (in seconds)

    Returns (avg_ttft, p50_ttft, p95_ttft, p99_ttft,
             avg_tpot, p50_tpot, p95_tpot, p99_tpot) all in milliseconds.
    """
    # GuideLLM path
    path = os.path.join(results_dir, "results.json")
    if os.path.isfile(path):
        with open(path) as f:
            data = json.load(f)
        metrics = data["benchmarks"][0]["metrics"]

        def _pct(section_key, pct):
            section = metrics.get(section_key, {}).get("successful", {})
            pcts = section.get("percentiles", {})
            if isinstance(pcts, list):
                pcts = {p["percentile"]: p["value"] for p in pcts}
            return pcts.get(pct) or pcts.get(str(pct))

        def _mean(section_key):
            return metrics.get(section_key, {}).get("successful", {}).get("mean")

        tpot_key = "time_per_output_token_ms" if "time_per_output_token_ms" in metrics else "inter_token_latency_ms"
        return (
            _mean("time_to_first_token_ms"),
            _pct("time_to_first_token_ms", "p50") or _pct("time_to_first_token_ms", 50),
            _pct("time_to_first_token_ms", "p95") or _pct("time_to_first_token_ms", 95),
            _pct("time_to_first_token_ms", "p99") or _pct("time_to_first_token_ms", 99),
            _mean(tpot_key),
            _pct(tpot_key, "p50") or _pct(tpot_key, 50),
            _pct(tpot_key, "p95") or _pct(tpot_key, 95),
            _pct(tpot_key, "p99") or _pct(tpot_key, 99),
        )

    # inference-perf path: summary_lifecycle_metrics.json (values in seconds)
    path = os.path.join(results_dir, "summary_lifecycle_metrics.json")
    if os.path.isfile(path):
        with open(path) as f:
            data = json.load(f)
        lat = data.get("successes", {}).get("latency", {})

        def _ms(section_key, stat):
            v = lat.get(section_key, {}).get(stat)
            return v * 1000.0 if v is not None else None

        return (
            _ms("time_to_first_token", "mean"),
            _ms("time_to_first_token", "median"),
            _ms("time_to_first_token", "p95"),
            _ms("time_to_first_token", "p99"),
            _ms("time_per_output_token", "mean"),
            _ms("time_per_output_token", "median"),
            _ms("time_per_output_token", "p95"),
            _ms("time_per_output_token", "p99"),
        )

    return (None,) * 8


def _extract_gpu_time_min(results_dir, gpus_per_replica, workload_duration_min=None):
    """GPU time in GPU·min = avg_replicas × gpus × workload_duration_min.

    workload_duration_min: explicit workload duration (use the scenario duration,
    e.g. 15 min for the prefill_rampup scenario).  When provided, all runs use the
    same denominator so the comparison is fair even if harness setup/teardown timing
    differs between runs.  When None, falls back to the WVA timeseries time span
    (less reliable for cross-run comparisons).

    Replica counts come from wva_target_timeseries.json primary field.
    """
    path = os.path.join(results_dir, "metrics", "processed",
                        "wva_target_timeseries.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        data = json.load(f)
    samples = [s for s in data.get("samples", []) if s.get("primary") is not None]
    if not samples:
        return None

    vals = [s["primary"] for s in samples]
    avg = sum(vals) / len(vals)

    if workload_duration_min is not None:
        duration = workload_duration_min
    elif len(samples) >= 2:
        duration = (samples[-1]["timestamp"] - samples[0]["timestamp"]) / 60.0
    else:
        return None

    return avg * gpus_per_replica * duration


def _extract_error_count(results_dir):
    """Error count from results.json."""
    path = os.path.join(results_dir, "results.json")
    if not os.path.isfile(path):
        return 0
    with open(path) as f:
        data = json.load(f)
    return data["benchmarks"][0]["metrics"]["request_totals"].get("errored", 0)


def _extract_replica_stats(results_dir):
    """Avg and max ready replicas.

    Primary source: replica_status_timeseries.json (decode controllers).
    Fallback: wva_target_timeseries.json primary field (WVA decisions), used
    when the Kubernetes replica scraper returns empty controllers (inference-perf).
    """
    path = os.path.join(results_dir, "metrics", "processed",
                        "replica_status_timeseries.json")
    if os.path.isfile(path):
        with open(path) as f:
            data = json.load(f)
        totals = []
        for snap in data["snapshots"]:
            ready = sum(
                (c.get("ready_replicas", 0) or 0) for c in snap["controllers"]
                if "decode" in c.get("name", "")
            )
            totals.append(ready)
        if any(t > 0 for t in totals):
            return mean(totals), max(totals)

    # Fallback: WVA target decisions (primary field)
    wva_path = os.path.join(results_dir, "metrics", "processed",
                            "wva_target_timeseries.json")
    if os.path.isfile(wva_path):
        with open(wva_path) as f:
            data = json.load(f)
        vals = [s["primary"] for s in data.get("samples", [])
                if s.get("primary") is not None]
        if vals:
            return mean(vals), max(vals)

    return None, None


def _extract_variant_replica_stats(results_dir, secondary_suffix):
    """Per-variant avg/max ready replicas from replica_status_timeseries.json.

    Returns (primary_avg, primary_max, secondary_avg, secondary_max).
    Controllers whose name ends with '-<secondary_suffix>' are secondary;
    all other decode controllers are primary.
    """
    path = os.path.join(results_dir, "metrics", "processed",
                        "replica_status_timeseries.json")
    if not os.path.isfile(path):
        return None, None, None, None

    with open(path) as f:
        data = json.load(f)

    primary_totals, secondary_totals = [], []
    for snap in data["snapshots"]:
        p, s = 0, 0
        for c in snap.get("controllers", []):
            name = c.get("name", "")
            ready = c.get("ready_replicas", 0) or 0
            if "decode" not in name:
                continue
            if f"-{secondary_suffix}" in name:
                s += ready
            else:
                p += ready
        primary_totals.append(p)
        secondary_totals.append(s)

    if not primary_totals:
        return None, None, None, None
    return (mean(primary_totals), max(primary_totals),
            mean(secondary_totals), max(secondary_totals))


def _extract_kv_cache_avg(results_dir):
    """Average KV cache utilization (%) from raw vLLM metrics."""
    raw_dir = os.path.join(results_dir, "metrics", "raw")
    if not os.path.isdir(raw_dir):
        return None

    values = []
    for fname in os.listdir(raw_dir):
        if not fname.endswith(".log") or "router-epp" in fname:
            continue
        if fname == "collection_debug.log":
            continue
        fpath = os.path.join(raw_dir, fname)
        with open(fpath) as f:
            for line in f:
                val = _parse_prometheus_value(line.strip(),
                                              "vllm:kv_cache_usage_perc")
                if val is not None:
                    values.append(val * 100)
                    break

    return mean(values) if values else None


def _extract_queue_depth_avg(results_dir):
    """Average EPP queue depth from raw EPP metrics."""
    raw_dir = os.path.join(results_dir, "metrics", "raw")
    if not os.path.isdir(raw_dir):
        return None

    values = []
    for fname in sorted(os.listdir(raw_dir)):
        if not fname.endswith(".log") or "router-epp" not in fname:
            continue
        fpath = os.path.join(raw_dir, fname)
        with open(fpath) as f:
            for line in f:
                val = _parse_prometheus_value(
                    line.strip(),
                    "inference_extension_flow_control_queue_size")
                if val is not None:
                    values.append(val)
                    break

    return mean(values) if values else None


def _extract_pod_startup_avg(results_dir):
    """Average pod startup time (s) from pod_startup_times.json."""
    path = os.path.join(results_dir, "metrics", "processed",
                        "pod_startup_times.json")
    if not os.path.isfile(path):
        return None

    with open(path) as f:
        data = json.load(f)

    times = [p["startup_seconds"] for p in data.get("pods", [])
             if p.get("startup_seconds") is not None]
    return mean(times) if times else None


def _fmt(metric, value):
    """Format a value to match the benchmark.md number style."""
    if value is None:
        return "?"

    if metric in ("Avg TTFT (ms)", "P50 TTFT (ms)", "P95 TTFT (ms)", "P99 TTFT (ms)"):
        return f"{value:,.0f}"
    if metric in ("Avg TPOT (ms/token)", "P50 TPOT (ms/token)",
                  "P95 TPOT (ms/token)", "P99 TPOT (ms/token)"):
        return f"{value:.2f}" if (value * 100) % 10 != 0 else f"{value:.1f}"
    if metric in ("Avg replicas",) or metric.startswith("Avg ") and "replicas" in metric:
        return f"{value:.2f}"
    if metric in ("Max replicas",) or metric.startswith("Max ") and "replicas" in metric:
        return str(int(value))
    if metric == "GPU time (GPU·min)":
        return f"{value:.1f}"
    if metric == "Avg KV cache utilization":
        return f"{value:.1f}%"
    if metric == "Avg queue depth (EPP)":
        return f"{value:.1f}"
    if metric == "Error count":
        return f"{int(value):,}"
    if metric == "Avg pod startup (s)":
        return str(round(value))
    if metric == "Cost (weighted avg replicas × GPU/hr)":
        return f"{value:.2f}"
    return str(value)


def process_one(results_dir, secondary_suffix=None, gpus_per_primary=1,
                gpus_per_secondary=1, gpus_per_replica=None,
                primary_label="primary", secondary_label="secondary",
                workload_duration_min=None):
    """Extract all benchmark.md metrics from one results directory.

    When secondary_suffix is given, replica stats are split per variant and a
    weighted cost row is included.

    gpus_per_replica: GPU count for single-variant GPU time calculation.
    If None, falls back to gpus_per_primary.
    """
    avg_ttft, p50_ttft, p95_ttft, p99_ttft, avg_tpot, p50_tpot, p95_tpot, p99_tpot = _extract_latency(results_dir)
    kv_avg = _extract_kv_cache_avg(results_dir)
    queue_avg = _extract_queue_depth_avg(results_dir)
    startup_avg = _extract_pod_startup_avg(results_dir)
    error_count = _extract_error_count(results_dir)

    if secondary_suffix:
        p_avg, p_max, s_avg, s_max = _extract_variant_replica_stats(
            results_dir, secondary_suffix)
        cost = None
        if p_avg is not None and s_avg is not None:
            cost = p_avg * gpus_per_primary + s_avg * gpus_per_secondary
        return {
            "Avg TTFT (ms)": avg_ttft,
            "P50 TTFT (ms)": p50_ttft,
            "P95 TTFT (ms)": p95_ttft,
            "P99 TTFT (ms)": p99_ttft,
            "Avg TPOT (ms/token)": avg_tpot,
            "P50 TPOT (ms/token)": p50_tpot,
            "P95 TPOT (ms/token)": p95_tpot,
            "P99 TPOT (ms/token)": p99_tpot,
            f"Avg {primary_label} replicas": p_avg,
            f"Max {primary_label} replicas": p_max,
            f"Avg {secondary_label} replicas": s_avg,
            f"Max {secondary_label} replicas": s_max,
            "Avg KV cache utilization": kv_avg,
            "Avg queue depth (EPP)": queue_avg,
            "Error count": error_count,
            "Avg pod startup (s)": startup_avg,
            "Cost (weighted avg replicas × GPU/hr)": cost,
        }

    avg_rep, max_rep = _extract_replica_stats(results_dir)
    gpus = gpus_per_replica if gpus_per_replica is not None else gpus_per_primary
    gpu_time = _extract_gpu_time_min(results_dir, gpus, workload_duration_min)
    return {
        "Avg TTFT (ms)": avg_ttft,
        "P50 TTFT (ms)": p50_ttft,
        "P95 TTFT (ms)": p95_ttft,
        "P99 TTFT (ms)": p99_ttft,
        "Avg TPOT (ms/token)": avg_tpot,
        "P50 TPOT (ms/token)": p50_tpot,
        "P95 TPOT (ms/token)": p95_tpot,
        "P99 TPOT (ms/token)": p99_tpot,
        "Avg replicas": avg_rep,
        "Max replicas": max_rep,
        "GPU time (GPU·min)": gpu_time,
        "Avg KV cache utilization": kv_avg,
        "Avg queue depth (EPP)": queue_avg,
        "Error count": error_count,
        "Avg pod startup (s)": startup_avg,
    }


def _compute_avg(runs, metrics):
    """Compute average column across multiple runs (raw numeric values)."""
    avg = {}
    for m in metrics:
        vals = [r[m] for r in runs if r.get(m) is not None]
        avg[m] = mean(vals) if vals else None
    return avg


def format_table(runs, labels, metrics=None):
    """Render a column-aligned markdown table (renders cleanly in editors)."""
    if metrics is None:
        metrics = METRICS
    show_avg = len(runs) > 2
    cols = list(labels)
    data_cols = list(runs)

    if show_avg:
        cols.append("Avg")
        data_cols.append(_compute_avg(runs, metrics))

    # Pre-format all cells so we can measure widths
    cell_grid = [[_fmt(m, run.get(m)) for run in data_cols] for m in metrics]

    # Column widths: max of header, separator min (6), and all cell values
    metric_w = max(len("Metric"), max(len(m) for m in metrics))
    col_ws = [
        max(6, len(col), max((len(cell_grid[r][c]) for r in range(len(metrics))), default=0))
        for c, col in enumerate(cols)
    ]

    def _row(left, cells, widths):
        padded = [c.ljust(w) for c, w in zip(cells, widths)]
        return "| " + left + " | " + " | ".join(padded) + " |"

    header = _row("Metric".ljust(metric_w), [c.ljust(w) for c, w in zip(cols, col_ws)], col_ws)
    sep = "| " + "-" * metric_w + " | " + " | ".join("-" * w for w in col_ws) + " |"

    rows = [header, sep]
    for m, cells in zip(metrics, cell_grid):
        rows.append(_row(m.ljust(metric_w), cells, col_ws))

    return "\n".join(rows)


def main():
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description="Post-process llm-d-benchmark results into a markdown table")
    ap.add_argument("results_dirs", nargs="+",
                    help="One or more benchmark results directories")
    ap.add_argument("--scenario", type=str, default=None,
                    help="Scenario heading (e.g. 'Prefill Heavy — Qwen/Qwen3-32B (600s)')")
    ap.add_argument("--json", action="store_true",
                    help="Output raw JSON instead of markdown")
    ap.add_argument("--secondary-suffix", type=str, default=None,
                    help="Controller-name suffix that identifies the secondary variant "
                         "(e.g. 'v2'); enables per-variant replica rows and weighted cost")
    ap.add_argument("--primary-label", type=str, default="primary",
                    help="Label for the primary variant in table rows (default: primary)")
    ap.add_argument("--secondary-label", type=str, default="secondary",
                    help="Label for the secondary variant in table rows (default: secondary)")
    ap.add_argument("--scenario-yaml", type=str, default=None,
                    help="Path to the primary scenario YAML; tensor: value sets gpus-per-primary")
    ap.add_argument("--variant-config", type=str, default=None,
                    help="Path to the secondary variant config YAML; tensor: value sets gpus-per-secondary")
    ap.add_argument("--gpus-per-replica", type=int, default=None,
                    help="GPU count per replica for single-variant GPU time calculation")
    ap.add_argument("--workload-duration-min", type=float, default=None,
                    help="Fixed workload duration in minutes for GPU time (use scenario "
                         "duration, e.g. 15 for prefill_rampup; ensures fair cross-run comparison)")
    ap.add_argument("--labels", nargs="+", default=None,
                    help="Custom column labels for each results directory (e.g. 'V1 Analyzer' 'V2 Analyzer')")
    args = ap.parse_args()

    gpus_per_primary = _read_tensor_from_yaml(args.scenario_yaml)
    gpus_per_secondary = _read_tensor_from_yaml(args.variant_config)

    metrics = (
        _variant_metrics(args.primary_label, args.secondary_label)
        if args.secondary_suffix
        else METRICS
    )

    runs = []
    labels = []
    for i, d in enumerate(args.results_dirs):
        if not os.path.isdir(d):
            print(f"WARNING: {d} is not a directory, skipping", file=sys.stderr)
            continue
        print(f"Processing: {d}", file=sys.stderr)
        runs.append(process_one(
            d,
            secondary_suffix=args.secondary_suffix,
            gpus_per_primary=gpus_per_primary,
            gpus_per_secondary=gpus_per_secondary,
            gpus_per_replica=args.gpus_per_replica,
            primary_label=args.primary_label,
            secondary_label=args.secondary_label,
            workload_duration_min=args.workload_duration_min,
        ))
        if args.labels and i < len(args.labels):
            labels.append(args.labels[i])
        else:
            labels.append(f"Run {len(runs)}")

    if not runs:
        print("ERROR: No valid results directories found", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(runs, indent=2))
        return

    print()
    if args.scenario:
        print(f"### {args.scenario}\n")
    print(format_table(runs, labels, metrics))
    print()


if __name__ == "__main__":
    main()
