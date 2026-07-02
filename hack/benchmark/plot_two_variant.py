#!/usr/bin/env python3
"""
Two-variant efficiency-aware benchmark visualizer.

Generates a 5-panel PNG from a llm-d-benchmark results directory produced by a
two-variant WVA run (primary + secondary variant, each with its own HPA).

Panels:
  1. Replica count over time — primary (blue) vs secondary (orange), EPP queue on right axis
  2. KV cache utilization — per-pod snapshot, grouped and colored by variant
  3. Request throughput — success vs error RPS from results.json
  4. TTFT — mean + P99 per benchmark phase (bar chart)
  5. ITL — mean + P99 per benchmark phase (bar chart)

Usage:
    python hack/benchmark/plot_two_variant.py <results_dir> [-o out.png]
    python hack/benchmark/plot_two_variant.py <results_dir> --secondary-suffix v2

    # Via Makefile (auto-detects latest results):
    make benchmark-plot-two-variant
"""

import argparse
import datetime
import json
import os
import sys
from statistics import mean

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np
except ImportError:
    print("ERROR: pip install matplotlib numpy", file=sys.stderr)
    sys.exit(1)

_C_PRIMARY = "#1f77b4"
_C_SECONDARY = "#ff7f0e"
_C_EPP = "darkorange"
_C_SUCCESS = "#2ca02c"
_C_ERROR = "#d62728"

_STEP_SECONDS = 15


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_replica_timeseries(results_dir, secondary_suffix):
    """Parse replica_status_timeseries.json into (timestamps, primary, secondary).

    timestamps is a list of datetime objects or sequential ints when the file
    has no timestamp field.  primary/secondary are lists of ready-replica counts.
    Returns (None, None, None) if the file is absent.
    """
    path = os.path.join(results_dir, "metrics", "processed",
                        "replica_status_timeseries.json")
    if not os.path.isfile(path):
        return None, None, None

    with open(path) as f:
        data = json.load(f)

    snaps = data.get("snapshots", [])
    if not snaps:
        return None, None, None

    timestamps, primary, secondary = [], [], []
    for i, snap in enumerate(snaps):
        ts = snap.get("timestamp") or snap.get("time") or i
        timestamps.append(ts)

        p_count, s_count = 0, 0
        for c in snap.get("controllers", []):
            name = c.get("name", "")
            ready = c.get("ready_replicas", 0) or 0
            if "decode" not in name:
                continue
            if secondary_suffix and name.endswith(f"-{secondary_suffix}"):
                s_count += ready
            else:
                p_count += ready
        primary.append(p_count)
        secondary.append(s_count)

    if timestamps and isinstance(timestamps[0], (int, float)) and timestamps[0] > 1e9:
        timestamps = [datetime.datetime.fromtimestamp(float(t)) for t in timestamps]

    return timestamps, primary, secondary


def _load_kv_cache_snapshot(results_dir, secondary_suffix):
    """Read per-pod KV-cache utilisation from metrics/raw/*.log.

    Returns (primary_vals, secondary_vals) where each is a list of (pod, %)
    tuples.  Reads the first matching vllm:kv_cache_usage_perc line per file.
    """
    raw_dir = os.path.join(results_dir, "metrics", "raw")
    if not os.path.isdir(raw_dir):
        return [], []

    primary_vals, secondary_vals = [], []
    for fname in sorted(os.listdir(raw_dir)):
        if not fname.endswith(".log") or "router-epp" in fname:
            continue
        if fname == "collection_debug.log":
            continue
        fpath = os.path.join(raw_dir, fname)
        pod = fname.replace(".log", "")
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if not line.startswith("vllm:kv_cache_usage_perc"):
                    continue
                rest = line[len("vllm:kv_cache_usage_perc"):]
                if rest and rest[0] not in ("{", " "):
                    continue
                if rest.startswith("{"):
                    close = rest.find("}")
                    if close < 0:
                        continue
                    rest = rest[close + 1:]
                try:
                    val = float(rest.strip()) * 100
                except ValueError:
                    continue
                if secondary_suffix and pod.endswith(f"-{secondary_suffix}"):
                    secondary_vals.append((pod, val))
                else:
                    primary_vals.append((pod, val))
                break

    return primary_vals, secondary_vals


def _load_epp_queue_snapshot(results_dir):
    """Read EPP queue size snapshot from metrics/raw/*.log. Returns [(pod, val)]."""
    raw_dir = os.path.join(results_dir, "metrics", "raw")
    if not os.path.isdir(raw_dir):
        return []
    vals = []
    for fname in sorted(os.listdir(raw_dir)):
        if not fname.endswith(".log") or "router-epp" not in fname:
            continue
        fpath = os.path.join(raw_dir, fname)
        pod = fname.replace(".log", "")
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if not line.startswith("inference_extension_flow_control_queue_size"):
                    continue
                rest = line[len("inference_extension_flow_control_queue_size"):]
                if rest.startswith("{"):
                    close = rest.find("}")
                    if close < 0:
                        continue
                    rest = rest[close + 1:]
                try:
                    vals.append((pod, float(rest.strip())))
                except ValueError:
                    continue
                break
    return vals


def _load_rps_timeseries(results_dir):
    """Build 15-second-bin RPS timeseries from results.json request timestamps.

    Returns (x_times, y_success_rps, y_error_rps) as parallel lists.
    """
    path = os.path.join(results_dir, "results.json")
    if not os.path.isfile(path):
        return [], [], []

    with open(path) as f:
        data = json.load(f)

    succ_ts, fail_ts = [], []
    for benchmark in data.get("benchmarks", []):
        reqs = benchmark.get("requests", {})
        for req in reqs.get("successful", []):
            t = req.get("request_start_time")
            if t is not None:
                succ_ts.append(t)
        for req in reqs.get("errored", []):
            t = req.get("request_start_time")
            if t is not None:
                fail_ts.append(t)

    all_ts = succ_ts + fail_ts
    if not all_ts:
        return [], [], []

    t_min, t_max = min(all_ts), max(all_ts)
    bins = {}
    t = t_min
    while t <= t_max + _STEP_SECONDS:
        bins[t] = {"s": 0, "e": 0}
        t += _STEP_SECONDS

    for ts in succ_ts:
        b = t_min + ((ts - t_min) // _STEP_SECONDS) * _STEP_SECONDS
        if b in bins:
            bins[b]["s"] += 1
    for ts in fail_ts:
        b = t_min + ((ts - t_min) // _STEP_SECONDS) * _STEP_SECONDS
        if b in bins:
            bins[b]["e"] += 1

    sorted_bins = sorted(bins)
    x = [datetime.datetime.fromtimestamp(b) for b in sorted_bins]
    y_s = [bins[b]["s"] / _STEP_SECONDS for b in sorted_bins]
    y_e = [bins[b]["e"] / _STEP_SECONDS for b in sorted_bins]
    return x, y_s, y_e


def _load_latency_summary(results_dir):
    """Return (labels, ttft_mean, ttft_p99, itl_mean, itl_p99) from results.json."""
    path = os.path.join(results_dir, "results.json")
    if not os.path.isfile(path):
        return [], [], [], [], []

    with open(path) as f:
        data = json.load(f)

    labels, ttft_mean, ttft_p99, itl_mean, itl_p99 = [], [], [], [], []
    for i, bm in enumerate(data.get("benchmarks", [])):
        rate = bm.get("config", {}).get("strategy", {}).get("rate")
        labels.append(f"{rate} RPS" if rate else f"Run {i + 1}")
        m = bm.get("metrics", {})

        def _extract(section_key):
            s = m.get(section_key, {}).get("successful", {})
            pcts = s.get("percentiles", {})
            if isinstance(pcts, list):
                pcts = {p["percentile"]: p["value"] for p in pcts}
            p99 = pcts.get("p99") or pcts.get(99) or s.get("max") or 0
            return s.get("mean", 0), p99

        tm, tp = _extract("time_to_first_token_ms")
        im, ip = _extract("inter_token_latency_ms")
        ttft_mean.append(tm)
        ttft_p99.append(tp)
        itl_mean.append(im)
        itl_p99.append(ip)

    return labels, ttft_mean, ttft_p99, itl_mean, itl_p99


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def _fmt_xaxis(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")


def plot(results_dir, secondary_suffix, output_path, scenario_title):
    timestamps, primary, secondary = _load_replica_timeseries(results_dir, secondary_suffix)
    kv_primary, kv_secondary = _load_kv_cache_snapshot(results_dir, secondary_suffix)
    epp_vals = _load_epp_queue_snapshot(results_dir)
    rps_x, rps_s, rps_e = _load_rps_timeseries(results_dir)
    labels, ttft_mean, ttft_p99, itl_mean, itl_p99 = _load_latency_summary(results_dir)

    has_replicas = timestamps is not None
    has_kv = bool(kv_primary or kv_secondary)
    has_rps = bool(rps_x)
    has_latency = bool(labels)

    if not any([has_replicas, has_kv, has_rps, has_latency]):
        print(f"WARNING: no plottable data found in {results_dir}", file=sys.stderr)
        sys.exit(1)

    fig, axes = plt.subplots(5, 1, figsize=(14, 22))
    if scenario_title:
        fig.suptitle(scenario_title, fontsize=13, fontweight="bold")

    ax_rep, ax_kv, ax_rps, ax_ttft, ax_itl = axes

    # ------------------------------------------------------------------
    # Panel 1: replica scaling
    # ------------------------------------------------------------------
    if has_replicas:
        use_dates = isinstance(timestamps[0], datetime.datetime)
        x = timestamps if use_dates else list(range(len(timestamps)))

        ax_rep.step(x, primary, where="post", color=_C_PRIMARY,
                    linewidth=2, label="Primary replicas")
        ax_rep.step(x, secondary, where="post", color=_C_SECONDARY,
                    linewidth=2, linestyle="--", label="Secondary replicas")
        ax_rep.set_ylabel("Ready replicas")
        ax_rep.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
        ax_rep.grid(True, linestyle="--", alpha=0.5)
        ax_rep.legend(loc="upper left")
        if use_dates:
            _fmt_xaxis(ax_rep)
        if epp_vals:
            ax_epp = ax_rep.twinx()
            avg_epp = mean(v for _, v in epp_vals)
            ax_epp.axhline(avg_epp, color=_C_EPP, linewidth=1.5,
                           linestyle=":", label=f"EPP queue (avg {avg_epp:.0f})")
            ax_epp.set_ylabel("EPP queue size", color=_C_EPP)
            ax_epp.tick_params(axis="y", labelcolor=_C_EPP)
            ax_epp.legend(loc="upper right")
    else:
        ax_rep.text(0.5, 0.5, "No replica timeseries data", ha="center", va="center",
                    transform=ax_rep.transAxes)
    ax_rep.set_title("Replica scaling — primary vs secondary")

    # ------------------------------------------------------------------
    # Panel 2: KV cache utilisation snapshot
    # ------------------------------------------------------------------
    if has_kv:
        pods = [p for p, _ in kv_primary] + [p for p, _ in kv_secondary]
        vals = [v for _, v in kv_primary] + [v for _, v in kv_secondary]
        colors = [_C_PRIMARY] * len(kv_primary) + [_C_SECONDARY] * len(kv_secondary)
        x_pos = np.arange(len(pods))
        ax_kv.bar(x_pos, vals, color=colors, alpha=0.8)
        ax_kv.set_xticks(x_pos)
        ax_kv.set_xticklabels([p.split("-")[-2] + "-" + p.split("-")[-1]
                                if "-" in p else p for p in pods],
                               rotation=45, ha="right", fontsize=8)
        ax_kv.set_ylim(0, 110)
        ax_kv.axhline(100, color="red", linewidth=0.8, linestyle="--")
        ax_kv.set_ylabel("KV cache utilisation (%)")
        from matplotlib.patches import Patch
        ax_kv.legend(handles=[
            Patch(color=_C_PRIMARY, label="Primary"),
            Patch(color=_C_SECONDARY, label="Secondary"),
        ], loc="upper right")
    else:
        ax_kv.text(0.5, 0.5, "No KV cache data", ha="center", va="center",
                   transform=ax_kv.transAxes)
    ax_kv.set_title("KV cache utilisation per pod (end-of-run snapshot)")
    ax_kv.grid(True, axis="y", linestyle="--", alpha=0.5)

    # ------------------------------------------------------------------
    # Panel 3: request throughput
    # ------------------------------------------------------------------
    if has_rps:
        total_s = int(sum(rps_s) * _STEP_SECONDS)
        total_e = int(sum(rps_e) * _STEP_SECONDS)
        ax_rps.plot(rps_x, rps_s, color=_C_SUCCESS, linewidth=1.5,
                    label=f"Successful (total {total_s:,})")
        if total_e > 0:
            ax_rps.plot(rps_x, rps_e, color=_C_ERROR, linewidth=1.5,
                        linestyle="--", label=f"Errors (total {total_e:,})")
        ax_rps.set_ylabel("Requests / second")
        ax_rps.legend(loc="upper right")
        ax_rps.grid(True, linestyle="--", alpha=0.5)
        _fmt_xaxis(ax_rps)
    else:
        ax_rps.text(0.5, 0.5, "No request timeseries data", ha="center", va="center",
                    transform=ax_rps.transAxes)
    ax_rps.set_title("Request throughput")

    # ------------------------------------------------------------------
    # Panel 4: TTFT
    # ------------------------------------------------------------------
    if has_latency:
        x_pos = np.arange(len(labels))
        w = 0.35
        ax_ttft.bar(x_pos - w / 2, ttft_mean, w, color="skyblue", label="Mean")
        ax_ttft.bar(x_pos + w / 2, ttft_p99, w, color="salmon", label="P99")
        ax_ttft.set_xticks(x_pos)
        ax_ttft.set_xticklabels(labels)
        ax_ttft.set_ylabel("TTFT (ms, log)")
        ax_ttft.set_yscale("log")
        ax_ttft.legend()
        ax_ttft.grid(True, axis="y", linestyle="--", alpha=0.5)
    else:
        ax_ttft.text(0.5, 0.5, "No latency data", ha="center", va="center",
                     transform=ax_ttft.transAxes)
    ax_ttft.set_title("Time-to-first-token (TTFT)")

    # ------------------------------------------------------------------
    # Panel 5: ITL
    # ------------------------------------------------------------------
    if has_latency:
        ax_itl.bar(x_pos - w / 2, itl_mean, w, color="lightgreen", label="Mean")
        ax_itl.bar(x_pos + w / 2, itl_p99, w, color="orchid", label="P99")
        ax_itl.set_xticks(x_pos)
        ax_itl.set_xticklabels(labels)
        ax_itl.set_ylabel("ITL (ms/token, log)")
        ax_itl.set_yscale("log")
        ax_itl.legend()
        ax_itl.grid(True, axis="y", linestyle="--", alpha=0.5)
    else:
        ax_itl.text(0.5, 0.5, "No latency data", ha="center", va="center",
                    transform=ax_itl.transAxes)
    ax_itl.set_title("Inter-token latency (ITL)")

    plt.tight_layout(rect=[0, 0, 1, 0.97] if scenario_title else [0, 0, 1, 1])
    plt.savefig(output_path, bbox_inches="tight", dpi=150)
    print(f"Plot saved to {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Two-variant benchmark visualizer")
    ap.add_argument("results_dir",
                    help="llm-d-benchmark results directory (guidellm-* folder)")
    ap.add_argument("-o", "--output", default=None,
                    help="Output PNG path (default: <results_dir>/two_variant_plot.png)")
    ap.add_argument("--secondary-suffix", default="v2",
                    help="Suffix that identifies the secondary variant controller "
                         "(default: v2, matches *-v2 controller names)")
    ap.add_argument("--scenario", default=None,
                    help="Scenario title shown at top of the plot")
    args = ap.parse_args()

    if not os.path.isdir(args.results_dir):
        print(f"ERROR: {args.results_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    output = args.output or os.path.join(args.results_dir, "two_variant_plot.png")
    plot(args.results_dir, args.secondary_suffix, output, args.scenario)


if __name__ == "__main__":
    main()
