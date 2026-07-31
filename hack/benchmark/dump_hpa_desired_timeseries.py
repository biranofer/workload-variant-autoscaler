#!/usr/bin/env python3
"""Convert the raw per-cycle HPA status snapshots (captured live during the
run by collect_metrics.sh's collect_hpa_status(), from
`kubectl get hpa -o json` — the same status.desiredReplicas field
kube-state-metrics exposes as kube_horizontalpodautoscaler_status_desired_replicas,
captured directly instead of via Prometheus/kube-state-metrics so it works
even without a working ServiceMonitor/scrape path) into the plot-friendly
per-variant timeseries used by plot_two_variant_pipeline.py:

    metrics/processed/hpa_desired_timeseries.json

Same shape as wva_target_timeseries.json's samples ({timestamp, primary, v2})
so the two are interchangeable overlays — this one works for ANY two-variant
scenario (WVA-driven or plain KEDA-EPP), since it reads the HPA object
directly rather than a WVA-specific Prometheus metric.

Usage
-----
  python hack/benchmark/dump_hpa_desired_timeseries.py <results>/<treatment>_<i>
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    args = ap.parse_args()
    rd = Path(args.results_dir).resolve()
    src = rd / "metrics" / "processed" / "hpa_status_timeseries.json"

    samples = []
    if src.is_file():
        data = json.loads(src.read_text())
        for snap in data.get("snapshots", []):
            ts = int(
                datetime.fromisoformat(snap["timestamp"].replace("Z", "+00:00"))
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
            primary = v2 = None
            for h in snap.get("hpas", []):
                target = h.get("target_name", "")
                if target.endswith("-v2"):
                    v2 = h.get("desired_replicas")
                else:
                    primary = h.get("desired_replicas")
            if primary is None and v2 is None:
                continue
            samples.append({"timestamp": ts, "primary": primary, "v2": v2})

    out = rd / "metrics" / "processed" / "hpa_desired_timeseries.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"samples": samples}, indent=2))
    print(f"Wrote {out} ({len(samples)} snapshots)")


if __name__ == "__main__":
    main()
