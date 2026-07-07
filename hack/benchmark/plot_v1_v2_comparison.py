#!/usr/bin/env python3
"""Compare WVA V1 vs V2 saturation analyzer behavior on a single-variant ramp-up run.

Produces a 3-panel figure overlaying V1 (dashed) and V2 (solid) on:
  1. Replica count over time
  2. KV cache utilization (%) over time
  3. Requests waiting (queue depth) over time

Vertical lines mark the rate-stage transitions (every STAGE_DURATION_S seconds).

Usage:
    python3 hack/benchmark/plot_v1_v2_comparison.py \\
        --v1 <results_dir_v1> \\
        --v2 <results_dir_v2> \\
        --output <output_dir>

The script reads:
  - metrics/processed/replica_status_timeseries.json   (replicas)
  - metrics/processed/capacity_demand_estimate.json    (KV cache %, queue)
"""

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

V1_COLOR = "#1f77b4"
V2_COLOR = "#d62728"

# Expected duration of each rate stage in seconds.
STAGE_DURATION_S = 300
STAGE_RATES = [5, 10, 15]

FILE_RE = re.compile(r"^(?P<pod>.+?)_(?P<ts>\d{10})_metrics\.log$")
KV_RE = re.compile(r"^vllm:kv_cache_usage_perc\{[^}]*\}\s+([0-9.eE+-]+)")
WAIT_RE = re.compile(r"^vllm:num_requests_waiting\{[^}]*\}\s+([0-9.eE+-]+)")


def _parse_iso(ts_str):
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def load_replica_timeseries(results_dir: Path):
    """Return list of (unix_ts, ready_replicas) for decode controllers."""
    p = results_dir / "metrics" / "processed" / "replica_status_timeseries.json"
    if not p.is_file():
        return []
    data = json.loads(p.read_text())
    out = []
    for snap in data["snapshots"]:
        ts = int(_parse_iso(snap["timestamp"]).timestamp())
        total = sum(
            (c.get("ready_replicas") or 0)
            for c in snap["controllers"]
            if "decode" in c.get("name", "")
        )
        out.append((ts, total))
    return out


def load_capacity_demand(results_dir: Path):
    """Return list of {timestamp, kv_pct, waiting} from capacity_demand_estimate.json.

    kv_pct: average KV cache utilization across all pods (0-100).
    waiting: total requests waiting (vLLM queue + EPP queue components).
    """
    p = results_dir / "metrics" / "processed" / "capacity_demand_estimate.json"
    if not p.is_file():
        return []
    samples = json.loads(p.read_text()).get("samples", [])
    out = []
    for s in samples:
        capacity = s.get("capacityRaw") or 0
        in_use = s.get("demandInUse") or 0
        waiting = (s.get("demandWaitingPods") or 0) + (s.get("demandEppQueue") or 0)
        kv_pct = (in_use / capacity * 100.0) if capacity > 0 else None
        out.append({
            "timestamp": s["timestamp"],
            "kv_pct": kv_pct,
            "waiting_tokens": waiting,
        })
    return out


def load_raw_kv_waiting(results_dir: Path):
    """Fallback: extract KV cache % and waiting count directly from raw vLLM pod logs.

    Returns list of (unix_ts, avg_kv_pct, sum_waiting).
    Used when capacity_demand_estimate.json is absent.
    """
    raw_dir = results_dir / "metrics" / "raw"
    if not raw_dir.is_dir():
        return []

    by_ts = defaultdict(lambda: {"kv": [], "waiting": 0.0})
    for f in sorted(raw_dir.glob("*_metrics.log")):
        m = FILE_RE.match(f.name)
        if not m:
            continue
        pod = m.group("pod")
        if "decode" not in pod or "gaie-epp" in pod or "router-epp" in pod:
            continue
        ts = int(m.group("ts"))
        try:
            text = f.read_text()
        except Exception:
            continue
        for line in text.splitlines():
            km = KV_RE.match(line)
            if km:
                by_ts[ts]["kv"].append(float(km.group(1)) * 100.0)
            wm = WAIT_RE.match(line)
            if wm:
                by_ts[ts]["waiting"] += float(wm.group(1))

    out = []
    for ts in sorted(by_ts):
        kv_vals = by_ts[ts]["kv"]
        kv_pct = sum(kv_vals) / len(kv_vals) if kv_vals else None
        out.append((ts, kv_pct, by_ts[ts]["waiting"]))
    return out


def _normalize(series, t0):
    """Shift timestamps so t0 = 0, return list of (elapsed_seconds, value)."""
    return [(ts - t0, v) for ts, v in series]


def _first_ts(repls, cd):
    candidates = []
    if repls:
        candidates.append(repls[0][0])
    if cd:
        candidates.append(cd[0]["timestamp"])
    return min(candidates) if candidates else None


def _stage_boundaries(t0, n_stages=len(STAGE_RATES) - 1):
    """X-positions (seconds from t0) for vertical stage-transition lines."""
    return [STAGE_DURATION_S * (i + 1) for i in range(n_stages)]


def plot(v1_dir: Path, v2_dir: Path, out_path: Path, title: str):
    # Load data
    v1_repls = load_replica_timeseries(v1_dir)
    v2_repls = load_replica_timeseries(v2_dir)

    v1_cd = load_capacity_demand(v1_dir)
    v2_cd = load_capacity_demand(v2_dir)

    # Fallback to raw logs if capacity_demand_estimate is missing
    v1_raw = load_raw_kv_waiting(v1_dir) if not v1_cd else []
    v2_raw = load_raw_kv_waiting(v2_dir) if not v2_cd else []

    # Align time axes to seconds from run start
    v1_t0 = _first_ts(v1_repls, v1_cd) or (v1_raw[0][0] if v1_raw else 0)
    v2_t0 = _first_ts(v2_repls, v2_cd) or (v2_raw[0][0] if v2_raw else 0)

    v1_rep_norm = _normalize(v1_repls, v1_t0)
    v2_rep_norm = _normalize(v2_repls, v2_t0)

    def _cd_series(cd, raw, t0):
        if cd:
            kv = [(s["timestamp"] - t0, s["kv_pct"]) for s in cd]
            waiting = [(s["timestamp"] - t0, s["waiting_tokens"]) for s in cd]
        else:
            kv = [(ts - t0, kv_pct) for ts, kv_pct, _ in raw]
            waiting = [(ts - t0, w) for ts, _, w in raw]
        return kv, waiting

    v1_kv, v1_wait = _cd_series(v1_cd, v1_raw, v1_t0)
    v2_kv, v2_wait = _cd_series(v2_cd, v2_raw, v2_t0)

    boundaries = _stage_boundaries(0)

    fig, axes = plt.subplots(3, 1, figsize=(10, 10), sharex=False)

    def _draw_panel(ax, v1_data, v2_data, ylabel, title_panel, ylim=None):
        if v1_data:
            xs1 = [x for x, _ in v1_data]
            ys1 = [y for _, y in v1_data]
            ax.step(xs1, ys1, where="post", color=V1_COLOR, linestyle="--",
                    linewidth=2, label="V1 (saturation_v1)")
        if v2_data:
            xs2 = [x for x, _ in v2_data]
            ys2 = [y for _, y in v2_data]
            ax.step(xs2, ys2, where="post", color=V2_COLOR, linestyle="-",
                    linewidth=2, label="V2 (saturation_v2)")
        for i, b in enumerate(boundaries):
            ax.axvline(b, color="gray", linestyle=":", linewidth=1.0, alpha=0.7)
            rate = STAGE_RATES[i + 1]
            ax.text(b + 5, ax.get_ylim()[1] * 0.95, f"{rate} RPS",
                    fontsize=8, color="gray", va="top")
        ax.set_ylabel(ylabel)
        ax.set_title(title_panel)
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(alpha=0.3)
        if ylim is not None:
            ax.set_ylim(*ylim)

    # Panel 1: Replica count
    _draw_panel(axes[0], v1_rep_norm, v2_rep_norm,
                "Replicas", "Ready Replicas Over Time")
    # Annotate first stage rate
    axes[0].text(5, axes[0].get_ylim()[1] * 0.95, f"{STAGE_RATES[0]} RPS",
                 fontsize=8, color="gray", va="top")

    # Panel 2: KV cache utilization
    _draw_panel(axes[1], [(x, y) for x, y in v1_kv if y is not None],
                [(x, y) for x, y in v2_kv if y is not None],
                "KV Cache %", "KV Cache Utilization", ylim=(0, 100))
    axes[1].text(5, 95, f"{STAGE_RATES[0]} RPS", fontsize=8, color="gray", va="top")

    # Panel 3: Requests waiting
    _draw_panel(axes[2],
                [(x, y) for x, y in v1_wait if y is not None],
                [(x, y) for x, y in v2_wait if y is not None],
                "Waiting (tokens)" if v1_cd or v2_cd else "Waiting (requests)",
                "Queue Depth (Requests Waiting)")
    axes[2].text(5, axes[2].get_ylim()[1] * 0.95, f"{STAGE_RATES[0]} RPS",
                 fontsize=8, color="gray", va="top")

    for ax in axes:
        ax.set_xlabel("Elapsed time (s)")
        max_x = max(
            (max((x for x, _ in v1_rep_norm), default=0),
             max((x for x, _ in v2_rep_norm), default=0),
             STAGE_DURATION_S * len(STAGE_RATES) + 60)
        )
        ax.set_xlim(0, max_x)

    fig.suptitle(title, fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    print(f"Wrote {out_path}")


def main():
    ap = argparse.ArgumentParser(
        description="Compare V1 vs V2 WVA analyzer on a single-variant ramp-up run")
    ap.add_argument("--v1", required=True, help="Results directory for the V1 run")
    ap.add_argument("--v2", required=True, help="Results directory for the V2 run")
    ap.add_argument("--output", default=None,
                    help="Output directory for the PNG (default: inside --v2 dir)")
    ap.add_argument("--name", default="v1_v2_comparison.png",
                    help="Output filename (default: v1_v2_comparison.png)")
    ap.add_argument("--title", default="WVA Saturation Analyzer: V1 vs V2 — Prefill Ramp-Up (5→10→15 RPS)",
                    help="Plot title")
    args = ap.parse_args()

    v1_dir = Path(args.v1).resolve()
    v2_dir = Path(args.v2).resolve()

    if args.output:
        out_dir = Path(args.output).resolve()
    else:
        out_dir = v2_dir / "metrics" / "graphs"

    plot(v1_dir, v2_dir, out_dir / args.name, args.title)


if __name__ == "__main__":
    main()
