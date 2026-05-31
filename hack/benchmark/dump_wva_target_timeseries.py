#!/usr/bin/env python3
"""Extract WVA's per-variant target replica decisions from controller logs
within a given results dir's run window, and write them to
``metrics/processed/wva_target_timeseries.json`` so the pipeline plot can
overlay them.

Source signal: the controller's structured log line
  "Applied saturation decision via shared cache" {"variant": ..., "target": N, ...}
emitted at every reconcile (default ~30 s). One sample per reconcile per
variant; we group by timestamp.

Usage
-----
  python hack/benchmark/dump_wva_target_timeseries.py \
      <results>/<treatment>_<i> -n NAMESPACE
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. pip install pyyaml", file=sys.stderr)
    sys.exit(1)


LINE_PAT = re.compile(
    r'^(?P<ts>\S+)\t\S+\tsaturation/engine\.go:\d+\t'
    r'Applied saturation decision via shared cache\t'
    r'(?P<json>\{.*\})$'
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", help="Path to .../results/<treatment>_<i>")
    ap.add_argument("-n", "--namespace", required=True)
    args = ap.parse_args()

    rd = Path(args.results_dir).resolve()
    meta_path = rd / "run_metadata.yaml"
    if not meta_path.is_file():
        print(f"ERROR: run_metadata.yaml not found in {rd}", file=sys.stderr)
        sys.exit(1)
    meta = yaml.safe_load(meta_path.read_text())

    def parse_iso(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    start = parse_iso(meta["harness_start"])
    stop = parse_iso(meta["harness_stop"])

    # Pull WVA logs covering the run window. We query "since" relative to now
    # plus a small buffer to ensure we capture the harness-start tick.
    now = datetime.now(timezone.utc)
    since_seconds = int((now - start).total_seconds()) + 90

    logs = subprocess.run(
        ["kubectl", "logs", "-n", args.namespace,
         "-l", "app.kubernetes.io/name=workload-variant-autoscaler",
         f"--since={since_seconds}s", "--tail=200000"],
        capture_output=True, text=True,
    ).stdout

    samples_by_ts = {}
    for line in logs.splitlines():
        m = LINE_PAT.match(line)
        if not m:
            continue
        try:
            ts_dt = parse_iso(m.group("ts"))
        except ValueError:
            continue
        if ts_dt < start or ts_dt > stop:
            continue
        try:
            d = json.loads(m.group("json"))
        except json.JSONDecodeError:
            continue
        variant = d.get("variant", "")
        target = d.get("target")
        if target is None:
            continue
        tag = "v2" if variant.endswith("-v2") else "primary"
        bucket = samples_by_ts.setdefault(int(ts_dt.timestamp()), {})
        bucket[tag] = int(target)

    samples = [
        {"timestamp": ts, "primary": b.get("primary"), "v2": b.get("v2")}
        for ts, b in sorted(samples_by_ts.items())
    ]

    out = rd / "metrics" / "processed" / "wva_target_timeseries.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"samples": samples}, indent=2))
    print(f"Wrote {out} ({len(samples)} snapshots, "
          f"window {start.isoformat()} -> {stop.isoformat()})")


if __name__ == "__main__":
    main()
