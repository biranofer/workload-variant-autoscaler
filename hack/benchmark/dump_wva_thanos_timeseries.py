#!/usr/bin/env python3
"""Fetch WVA's own Prometheus metrics for a run's time window directly from
Thanos, into the same shape plot_two_variant_pipeline.py's
wva_metrics_per_variant() already reads:

    metrics/processed/wva_metrics_timeseries.json

This is an alternative to dump_wva_full_timeseries.py for setups where the
benchmark's own raw-scrape mechanism (collect_metrics.sh) doesn't include a
WVA-pod scrape block, but the cluster's Prometheus/Thanos does have working
scrape targets for the WVA ServiceMonitor (confirmed via `up{job="..."}=1`).

Requires running from a pod with network access to
thanos-querier.openshift-monitoring.svc.cluster.local:9091 and a bearer
token with read access (e.g. exec into the harness data-access pod).

Usage
-----
  oc exec -n <namespace> <data-access-pod> -- python3 - <<'EOF'
  # (paste script, or copy it into the pod first)
  EOF

  Or, simpler, run locally with `oc exec ... -- curl` piped through this
  script via --thanos-json (see main()).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

METRICS = [
    "wva_saturation_utilization",
    "wva_kv_cache_tokens_used",
    "wva_kv_cache_tokens_capacity",
    "wva_required_capacity",
    "wva_spare_capacity",
]


def thanos_query_range(data_pod, namespace, token, metric, variant_name, start, end, step=15):
    query = f'{metric}{{namespace="{namespace}", variant_name="{variant_name}"}}'
    cmd = [
        "oc", "exec", "-n", namespace, data_pod, "--",
        "curl", "-sk", "-H", f"Authorization: Bearer {token}",
        "--data-urlencode", f"query={query}",
        "--data-urlencode", f"start={start}",
        "--data-urlencode", f"end={end}",
        "--data-urlencode", f"step={step}",
        "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091/api/v1/query_range",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    if data.get("status") != "success":
        return []
    result = data["data"]["result"]
    if not result:
        return []
    # Single series expected (one variant, one accelerator).
    return [(float(ts), float(val)) for ts, val in result[0]["values"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", help="Path to .../results/<treatment>_<i>")
    ap.add_argument("-n", "--namespace", required=True)
    ap.add_argument("--variant-name", required=True,
                     help="ScaledObject/variant name, e.g. biran-keda-epp")
    ap.add_argument("--data-pod", required=True,
                     help="Pod to exec curl from (needs cluster network access)")
    ap.add_argument("--token", required=True, help="Bearer token for Thanos")
    args = ap.parse_args()

    rd = Path(args.results_dir).resolve()
    meta = yaml.safe_load((rd / "run_metadata.yaml").read_text())
    start = int(__import__("datetime").datetime.fromisoformat(
        meta["harness_start"].replace("Z", "+00:00")).timestamp())
    end = int(__import__("datetime").datetime.fromisoformat(
        meta["harness_stop"].replace("Z", "+00:00")).timestamp())

    series = {}
    for metric in METRICS:
        pts = thanos_query_range(args.data_pod, args.namespace, args.token,
                                  metric, args.variant_name, start, end)
        for ts, val in pts:
            series.setdefault(int(ts), {})[metric] = val

    samples = []
    for ts in sorted(series.keys()):
        samples.append({"timestamp": ts, "primary": series[ts], "v2": {}})

    out = rd / "metrics" / "processed" / "wva_metrics_timeseries.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"samples": samples}, indent=2))
    print(f"Wrote {out} ({len(samples)} samples)", file=sys.stderr)


if __name__ == "__main__":
    main()
