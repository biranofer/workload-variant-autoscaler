#!/usr/bin/env python3
"""Generate a per-cycle WVA decision table from the captured debug log.

Reads  <results_dir>/wva-debug-run.log  (captured by benchmark-run Makefile)
and emits  <results_dir>/wva_decision_table.txt  with one row per reconcile
cycle that contains a scale decision, plus a row for every no-change cycle.

Columns
-------
Time        HH:MM:SS UTC
K1_pri      k1 for primary (median across pods)
K1_v2       k1 for v2
K2_pri      k2 for primary (median across pods)
K2_v2       k2 for v2
K2src_pri   priority that produced k2 (P1=observed / P2=history / P3=derived / P4=fallback)
K2src_v2    same for v2
Demand      totalDemand from analyzer (tokens)
Util        utilisation % reported by analyzer
Eff_pri     cost-efficiency of primary (cost/prc, lower=better)
Eff_v2      cost-efficiency of v2
Decision    e.g. V2+3 P+1 NC P-2 V2-5
"""
import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

# Log-line patterns (LEVEL(-4) debug lines from our instrumented image)
RE_PER_REPLICA = re.compile(
    r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+LEVEL\(-4\).*Per-replica capacity'
    r'.*"pod":\s*"([^"]+)".*"variant":\s*"([^"]+)"'
    r'.*"k1":\s*(\d+).*"k2":\s*(\d+).*"effectiveCapacity":\s*(\d+)'
    r'.*"tokensInUse":\s*(\d+).*"queueLen":\s*(\d+)'
)
RE_VARIANT_AGG = re.compile(
    r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+LEVEL\(-4\).*Variant capacity aggregated'
    r'.*"variant":\s*"([^"]+)"'
    r'.*"cost":\s*([\d.]+).*"perReplicaCapacity":\s*([\d.]+)'
    r'.*"readyCount":\s*(\d+)'
)
RE_K2_SOURCE = re.compile(
    r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+LEVEL\(-4\).*k2-source'
    r'.*"priority":\s*(\d+).*"source":\s*"([^"]+)"'
    r'.*"k2":\s*(\d+).*"historyKey":\s*"([^"]+)"'
)
RE_ANALYZER = re.compile(
    r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+INFO.*V2 saturation analysis completed'
    r'.*"totalDemand":\s*([\d.]+).*"utilization":\s*([\d.]+)'
)
RE_DECISION = re.compile(
    r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+INFO.*Applied saturation decision'
    r'.*"variant":\s*"[^/]+/([^"]+)".*"action":\s*"([^"]+)".*"target":\s*(\d+)'
)

P_LABEL = {1: "P1-obs", 2: "P2-hist", 3: "P3-deriv", 4: "P4-k1"}


def is_v2(name: str) -> bool:
    return name.endswith("-v2")


def shorten(name: str) -> str:
    return "v2" if is_v2(name) else "pri"


def median(vals):
    if not vals:
        return 0
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def parse_log(log_path: Path):
    """Returns list of reconcile-cycle dicts, one per 30s window."""
    if not log_path.exists():
        return []

    # Bucket all events by 30s timestamp slot
    slots = defaultdict(lambda: {
        "k1": {"pri": [], "v2": []},
        "k2": {"pri": [], "v2": []},
        "k2src": {"pri": [], "v2": []},
        "prc": {"pri": None, "v2": None},
        "cost": {"pri": None, "v2": None},
        "demand": None,
        "util": None,
        "decisions": [],  # (variant, action, target)
    })

    with log_path.open() as f:
        for line in f:
            # Per-replica capacity
            m = RE_PER_REPLICA.match(line)
            if m:
                ts, pod, variant, k1, k2, eff, tiu, ql = (
                    m.group(1), m.group(2), m.group(3),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)),
                    int(m.group(7)), int(m.group(8))
                )
                slot = ts[:19]  # second-level slot = one reconcile cycle
                tag = shorten(variant)
                slots[slot]["k1"][tag].append(k1)
                slots[slot]["k2"][tag].append(k2)
                continue

            # k2 source
            m = RE_K2_SOURCE.match(line)
            if m:
                ts, prio, src, k2, hkey = (
                    m.group(1), int(m.group(2)), m.group(3),
                    int(m.group(4)), m.group(5)
                )
                slot = ts[:19]
                tag = "v2" if "|1|" not in hkey else "pri"
                # gpuCount=2 → primary, gpuCount=1 → v2 (based on historyKey)
                if "|2|" in hkey:
                    tag = "pri"
                elif "|1|" in hkey:
                    tag = "v2"
                label = P_LABEL.get(prio, f"P{prio}")
                slots[slot]["k2src"][tag].append(label)
                continue

            # Variant aggregated
            m = RE_VARIANT_AGG.match(line)
            if m:
                ts, variant, cost, prc, ready = (
                    m.group(1), m.group(2),
                    float(m.group(3)), float(m.group(4)), int(m.group(5))
                )
                slot = ts[:19]
                tag = shorten(variant)
                slots[slot]["prc"][tag] = prc
                slots[slot]["cost"][tag] = cost
                continue

            # Analyzer output
            m = RE_ANALYZER.match(line)
            if m:
                ts, demand, util = m.group(1), float(m.group(2)), float(m.group(3))
                slot = ts[:19]
                slots[slot]["demand"] = demand
                slots[slot]["util"] = util
                continue

            # Decision
            m = RE_DECISION.match(line)
            if m:
                ts, variant, action, target = (
                    m.group(1), m.group(2), m.group(3), int(m.group(4))
                )
                slot = ts[:19]
                slots[slot]["decisions"].append((variant, action, int(target)))

    rows = []
    for slot in sorted(slots.keys()):
        s = slots[slot]
        k1_pri = int(median(s["k1"]["pri"])) if s["k1"]["pri"] else 0
        k1_v2  = int(median(s["k1"]["v2"]))  if s["k1"]["v2"]  else 0
        k2_pri = int(median(s["k2"]["pri"])) if s["k2"]["pri"] else 0
        k2_v2  = int(median(s["k2"]["v2"]))  if s["k2"]["v2"]  else 0
        src_pri = s["k2src"]["pri"][-1] if s["k2src"]["pri"] else "?"
        src_v2  = s["k2src"]["v2"][-1]  if s["k2src"]["v2"]  else "?"
        prc_pri = s["prc"]["pri"] or k2_pri or 1
        prc_v2  = s["prc"]["v2"]  or k2_v2  or 1
        cost_pri = s["cost"]["pri"] or 10.0
        cost_v2  = s["cost"]["v2"]  or 5.0
        eff_pri = cost_pri / prc_pri if prc_pri > 0 else math.inf
        eff_v2  = cost_v2  / prc_v2  if prc_v2  > 0 else math.inf

        # Build decision string
        dec_parts = []
        for variant, action, target in s["decisions"]:
            tag = "V2" if is_v2(variant) else "P"
            if action == "scale-up":
                dec_parts.append(f"{tag}→{target}")
            elif action == "scale-down":
                dec_parts.append(f"{tag}→{target}")
        decision = " ".join(dec_parts) if dec_parts else "NC"

        rows.append({
            "time": slot[11:19] + "Z",
            "k1_pri": k1_pri, "k1_v2": k1_v2,
            "k2_pri": k2_pri, "k2_v2": k2_v2,
            "src_pri": src_pri, "src_v2": src_v2,
            "demand": int(s["demand"]) if s["demand"] else 0,
            "util": f"{s['util']*100:.0f}%" if s["util"] is not None else "?",
            "eff_pri": f"{eff_pri:.2e}", "eff_v2": f"{eff_v2:.2e}",
            "decision": decision,
        })
    return rows


def render_table(rows):
    if not rows:
        return "No data found in wva-debug-run.log"
    hdr = f"{'Time':10}  {'K1_pri':>8}  {'K1_v2':>7}  {'K2_pri':>8}  {'K2_v2':>7}  " \
          f"{'K2src_pri':>9}  {'K2src_v2':>9}  {'Demand':>10}  {'Util':>5}  " \
          f"{'Eff_pri':>9}  {'Eff_v2':>9}  Decision"
    sep = "-" * len(hdr)
    lines = [hdr, sep]
    for r in rows:
        lines.append(
            f"{r['time']:10}  {r['k1_pri']:>8,}  {r['k1_v2']:>7,}  "
            f"{r['k2_pri']:>8,}  {r['k2_v2']:>7,}  "
            f"{r['src_pri']:>9}  {r['src_v2']:>9}  "
            f"{r['demand']:>10,}  {r['util']:>5}  "
            f"{r['eff_pri']:>9}  {r['eff_v2']:>9}  {r['decision']}"
        )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    args = ap.parse_args()
    rd = Path(args.results_dir)
    log_file = rd / "wva-debug-run.log"
    rows = parse_log(log_file)
    table = render_table(rows)
    out = rd / "metrics" / "processed" / "wva_decision_table.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(table + "\n")
    print(f"Wrote {out} ({len(rows)} rows)")
    print(table[:2000] + ("..." if len(table) > 2000 else ""))


if __name__ == "__main__":
    main()
