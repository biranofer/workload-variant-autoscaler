#!/usr/bin/env python3
"""Generate the two-variant V2 full-pipeline 5-panel timeseries plot.

Mirrors `two_variant_v2_full_pipeline_v3.png` from biran-20260527-101013-246.
Panels: Replica Count | KV Cache Util (avg per variant) | Requests Running
(sum per variant) | vLLM Requests Waiting (sum per variant) | EPP Queue Metrics.
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

PRIMARY_COLOR = "#1f77b4"
V2_COLOR = "#d62728"

VLLM_METRICS = {
    "kv": re.compile(r"^vllm:kv_cache_usage_perc\{[^}]*\}\s+([0-9.eE+-]+)"),
    "running": re.compile(r"^vllm:num_requests_running\{[^}]*\}\s+([0-9.eE+-]+)"),
    "waiting": re.compile(r"^vllm:num_requests_waiting\{[^}]*\}\s+([0-9.eE+-]+)"),
}
EPP_METRICS = {
    "fc_queue": re.compile(
        r"^inference_extension_flow_control_queue_size\{[^}]*\}\s+([0-9.eE+-]+)"
    ),
    "pool_avg": re.compile(
        r"^inference_pool_average_queue_size\{[^}]*\}\s+([0-9.eE+-]+)"
    ),
    "per_pod": re.compile(
        r'^inference_pool_per_pod_queue_size\{model_server_pod="([^"]+)"[^}]*\}\s+([0-9.eE+-]+)'
    ),
}

FILE_RE = re.compile(r"^(?P<pod>.+?)_(?P<ts>\d{10})_metrics\.log$")


def parse_pod_log(path: Path):
    """Extract vllm metrics from a single decode pod log. Returns dict or None."""
    try:
        text = path.read_text()
    except Exception:
        return None
    if '"object":"error"' in text:
        return None
    out = {}
    for line in text.splitlines():
        for k, rx in VLLM_METRICS.items():
            if k in out:
                continue
            m = rx.match(line)
            if m:
                out[k] = float(m.group(1))
    return out or None


def parse_epp_log(path: Path):
    try:
        text = path.read_text()
    except Exception:
        return None
    fc = pa = None
    per_pod = defaultdict(float)
    for line in text.splitlines():
        if fc is None:
            m = EPP_METRICS["fc_queue"].match(line)
            if m:
                fc = float(m.group(1))
                continue
        if pa is None:
            m = EPP_METRICS["pool_avg"].match(line)
            if m:
                pa = float(m.group(1))
                continue
        m = EPP_METRICS["per_pod"].match(line)
        if m:
            per_pod[m.group(1)] = float(m.group(2))
    return {
        "fc_queue": fc or 0.0,
        "pool_avg": pa or 0.0,
        "per_pod": dict(per_pod),
    }


def collect(raw_dir: Path):
    decode_series = defaultdict(list)  # ts -> list of (pod, metrics dict)
    epp_series = []  # list of (ts, epp_dict)
    for f in sorted(raw_dir.glob("*_metrics.log")):
        m = FILE_RE.match(f.name)
        if not m:
            continue
        ts = int(m.group("ts"))
        pod = m.group("pod")
        if "gaie-epp" in pod:
            ed = parse_epp_log(f)
            if ed:
                epp_series.append((ts, ed))
        elif "decode" in pod:
            md = parse_pod_log(f)
            if md is None:
                continue
            decode_series[ts].append((pod, md))
    return decode_series, epp_series


def is_v2(pod_name: str) -> bool:
    return "-decode-v2-" in pod_name


def aggregate_decode(decode_series):
    """Per timestamp: avg KV (per variant), sum running, sum waiting."""
    rows = []
    for ts in sorted(decode_series.keys()):
        kvs = {"primary": [], "v2": []}
        runs = {"primary": 0.0, "v2": 0.0}
        waits = {"primary": 0.0, "v2": 0.0}
        for pod, m in decode_series[ts]:
            tag = "v2" if is_v2(pod) else "primary"
            if "kv" in m:
                kvs[tag].append(m["kv"])
            if "running" in m:
                runs[tag] += m["running"]
            if "waiting" in m:
                waits[tag] += m["waiting"]
        rows.append({
            "ts": ts,
            "kv_primary": (sum(kvs["primary"]) / len(kvs["primary"]) * 100.0)
                if kvs["primary"] else None,
            "kv_v2": (sum(kvs["v2"]) / len(kvs["v2"]) * 100.0)
                if kvs["v2"] else None,
            "run_primary": runs["primary"],
            "run_v2": runs["v2"],
            "wait_primary": waits["primary"],
            "wait_v2": waits["v2"],
        })
    return rows


def epp_panels(epp_series):
    rows = []
    for ts, ed in sorted(epp_series, key=lambda x: x[0]):
        per_pod = ed["per_pod"]
        sum_p = sum(v for k, v in per_pod.items() if not is_v2(k))
        sum_v = sum(v for k, v in per_pod.items() if is_v2(k))
        rows.append({
            "ts": ts,
            "fc_queue": ed["fc_queue"],
            "pool_avg": ed["pool_avg"],
            "per_pod_primary": sum_p,
            "per_pod_v2": sum_v,
        })
    return rows


def replica_timeseries(results_dir: Path):
    p = results_dir / "metrics" / "processed" / "replica_status_timeseries.json"
    snaps = json.loads(p.read_text())["snapshots"]
    out = []
    for s in snaps:
        ts = int(datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")).timestamp())
        prim = v2 = 0
        for c in s["controllers"]:
            if c["name"].endswith("-v2"):
                v2 = c["ready_replicas"]
            else:
                prim = c["ready_replicas"]
        out.append((ts, prim, v2))
    return out


def wva_target_timeseries(results_dir: Path):
    """Optional overlay: WVA's per-variant target decisions. Returns [] if not present."""
    p = results_dir / "metrics" / "processed" / "wva_target_timeseries.json"
    if not p.is_file():
        return []
    samples = json.loads(p.read_text()).get("samples", [])
    return [(int(s["timestamp"]), s.get("primary"), s.get("v2")) for s in samples]


def wva_supply_demand_timeseries(results_dir: Path):
    """WVA-side analyzer numbers (totalSupply/Demand etc.). Returns [] if absent.
    Output rows: (ts, supply, demand, util, required, spare).
    """
    p = results_dir / "metrics" / "processed" / "wva_target_timeseries.json"
    if not p.is_file():
        return []
    samples = json.loads(p.read_text()).get("samples", [])
    rows = []
    for s in samples:
        if s.get("totalSupply") is None and s.get("totalDemand") is None:
            continue
        rows.append((
            int(s["timestamp"]),
            s.get("totalSupply"),
            s.get("totalDemand"),
            s.get("utilization"),
            s.get("requiredCapacity"),
            s.get("spareCapacity"),
        ))
    return rows


def capacity_demand_estimate(results_dir: Path):
    """Estimated capacity & 3-component demand from raw vLLM/EPP scrapes.
    Returns [] if not present."""
    p = results_dir / "metrics" / "processed" / "capacity_demand_estimate.json"
    if not p.is_file():
        return []
    return json.loads(p.read_text()).get("samples", [])


def to_dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def plot(results_dir: Path, out_path: Path, title_suffix: str):
    decode_series, epp_series = collect(results_dir / "metrics" / "raw")
    drows = aggregate_decode(decode_series)
    erows = epp_panels(epp_series)
    repls = replica_timeseries(results_dir)
    wva_targets = wva_target_timeseries(results_dir)
    wva_sd = wva_supply_demand_timeseries(results_dir)
    cd_est = capacity_demand_estimate(results_dir)

    has_supply_demand = bool(wva_sd or cd_est)
    n_panels = 5 + (1 if has_supply_demand else 0)
    fig, axes = plt.subplots(n_panels, 1, figsize=(8, 11 + (2 if has_supply_demand else 0)),
                             sharex=True)
    # Panel offset for the original 5 panels: 0 if no supply/demand panel, 1 otherwise.
    base = 1 if has_supply_demand else 0

    # 1. Replica Count (actual ready) + optional overlay of WVA target decisions
    ax = axes[0]
    title = "Replica Count"
    if wva_targets:
        title += " — solid: ready,  dashed: WVA desired"
    ax.set_title(title)
    if repls:
        x = [to_dt(r[0]) for r in repls]
        ax.step(x, [r[1] for r in repls], where="post", color=PRIMARY_COLOR, label="primary (ready)", linewidth=2)
        ax.step(x, [r[2] for r in repls], where="post", color=V2_COLOR, label="v2 (ready)", linewidth=2)
    if wva_targets:
        xt = [to_dt(t[0]) for t in wva_targets]
        prim_t = [t[1] for t in wva_targets]
        v2_t = [t[2] for t in wva_targets]
        ax.step(xt, prim_t, where="post", color=PRIMARY_COLOR, linestyle="--", linewidth=1.4,
                label="primary (WVA target)", alpha=0.8)
        ax.step(xt, v2_t, where="post", color=V2_COLOR, linestyle="--", linewidth=1.4,
                label="v2 (WVA target)", alpha=0.8)
    ax.set_ylabel("Replicas")
    ax.legend(loc="upper left", fontsize=7)
    ax.grid(alpha=0.3)

    # 1b. (Optional) Estimated Capacity & Demand — tokens
    # Continuous lines come from raw-scrape estimate (always available when
    # raw scrapes exist); WVA-analyzer numbers from the controller log are
    # overlaid as markers when present, since they typically only cover the
    # subset of reconciles whose log lines were still in the buffer at dump
    # time.
    if has_supply_demand:
        ax = axes[1]
        ax.set_title("Estimated Capacity & Demand  (raw vLLM + EPP scrapes; ●  = WVA analyzer)")
        if cd_est:
            x = [to_dt(r["timestamp"]) for r in cd_est]
            cap = [r["capacityRaw"] for r in cd_est]
            d_in_use = [r["demandInUse"] for r in cd_est]
            d_with_wait = [r["demandInUse"] + r["demandWaitingPods"] for r in cd_est]
            d_total = [r["demandTotalEstimate"] for r in cd_est]
            ax.plot(x, cap, color="black", label="capacity (Σ num_gpu_blocks·block_size)",
                    linewidth=2)
            ax.plot(x, d_in_use, color="#1f77b4", label="demand: in-use (KV occupancy)",
                    linewidth=1.4)
            ax.plot(x, d_with_wait, color="#ff7f0e", label="demand: + vLLM waiting queue",
                    linewidth=1.4, linestyle="--")
            ax.plot(x, d_total, color="#d62728", label="demand: + EPP queue (total est.)",
                    linewidth=2, linestyle="-")
        if wva_sd:
            xt = [to_dt(r[0]) for r in wva_sd]
            sup = [r[1] for r in wva_sd if r[1] is not None]
            xs_sup = [to_dt(r[0]) for r in wva_sd if r[1] is not None]
            dem = [r[2] for r in wva_sd if r[2] is not None]
            xs_dem = [to_dt(r[0]) for r in wva_sd if r[2] is not None]
            if sup:
                ax.scatter(xs_sup, sup, color="black", marker="o", s=22, zorder=5,
                           label="WVA totalSupply")
            if dem:
                ax.scatter(xs_dem, dem, color="#d62728", marker="o", s=22, zorder=5,
                           label="WVA totalDemand")
        ax.set_ylabel("Tokens")
        ax.legend(loc="upper left", fontsize=7)
        ax.grid(alpha=0.3)
        ax.ticklabel_format(axis="y", style="sci", scilimits=(0, 0))

    # 2. KV Cache Utilization
    ax = axes[1 + base]
    ax.set_title("KV Cache Utilization (avg per variant)")
    if drows:
        x = [to_dt(r["ts"]) for r in drows]
        ax.plot(x, [r["kv_primary"] for r in drows], color=PRIMARY_COLOR, label="primary")
        ax.plot(x, [r["kv_v2"] for r in drows], color=V2_COLOR, label="v2")
    ax.set_ylabel("KV %")
    ax.set_ylim(0, 100)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)

    # 3. Requests Running
    ax = axes[2 + base]
    ax.set_title("Requests Running (sum per variant)")
    if drows:
        x = [to_dt(r["ts"]) for r in drows]
        ax.plot(x, [r["run_primary"] for r in drows], color=PRIMARY_COLOR, label="primary")
        ax.plot(x, [r["run_v2"] for r in drows], color=V2_COLOR, label="v2")
    ax.set_ylabel("Running")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    # 4. Requests Waiting
    ax = axes[3 + base]
    ax.set_title("vLLM Requests Waiting (sum per variant)")
    if drows:
        x = [to_dt(r["ts"]) for r in drows]
        ax.plot(x, [r["wait_primary"] for r in drows], color=PRIMARY_COLOR, label="primary")
        ax.plot(x, [r["wait_v2"] for r in drows], color=V2_COLOR, label="v2")
    ax.set_ylabel("Waiting")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    # 5. EPP Queue
    ax = axes[4 + base]
    ax.set_title("EPP Queue Metrics (single y-axis, all in same units)")
    if erows:
        x = [to_dt(r["ts"]) for r in erows]
        ax.plot(x, [r["fc_queue"] for r in erows], color="black", label="flow_control_queue (gateway)")
        ax.plot(x, [r["pool_avg"] for r in erows], color="orange", label="pool_average_queue", alpha=0.8)
        ax.plot(x, [r["per_pod_primary"] for r in erows], color=PRIMARY_COLOR, linestyle="--", label="per pod sum: primary")
        ax.plot(x, [r["per_pod_v2"] for r in erows], color=V2_COLOR, linestyle="--", label="per pod sum: v2")
    ax.set_ylabel("Requests in queue")
    ax.set_xlabel("Time (UTC)")
    ax.legend(loc="upper left", fontsize=7)
    ax.grid(alpha=0.3)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone.utc))

    final_prim = repls[-1][1] if repls else 0
    final_v2 = repls[-1][2] if repls else 0
    fig.suptitle(
        f"Two-Variant V2 — FULL PIPELINE {title_suffix}\n"
        f"primary={final_prim}, v2={final_v2}  cost-aware",
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out_path, dpi=120)
    print(f"Wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", help="Path to .../results/<treatment>_<i>")
    ap.add_argument("--name", default="two_variant_v2_full_pipeline.png")
    ap.add_argument("--suffix", default="")
    args = ap.parse_args()
    rd = Path(args.results_dir).resolve()
    out = rd / "metrics" / "graphs" / args.name
    out.parent.mkdir(parents=True, exist_ok=True)
    plot(rd, out, args.suffix)


if __name__ == "__main__":
    main()
