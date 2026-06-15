#!/usr/bin/env bash
# run_wva_benchmark.sh — run a clean two-variant WVA benchmark and generate analysis
#
# Usage:
#   ./hack/benchmark/run_wva_benchmark.sh -n NAMESPACE [options]
#
# Options:
#   -n NAMESPACE     Kubernetes namespace (required)
#   -m MODEL_ID      Model ID (default: unsloth/Meta-Llama-3.1-8B)
#   -s SPEC          llmdbenchmark spec (default: guides/two-variant-wva)
#   -w WORKLOAD      Scenario file in test/benchmark/scenarios/ (default: prefill_heavy.yaml)
#   -t               Generate per-reconcile WVA decision table (requires debug image)
#   -h               Show this help
#
# What this script does:
#   1. Stops the WVA controller and waits for pod deletion
#   2. Scales both model deployments to 1/1 (no WVA competition)
#   3. Waits for HPA to confirm clean 1/1 state
#   4. Starts a fresh WVA controller pod
#   5. Begins in-flight WVA log capture to /tmp/wva-debug-run.log
#   6. Verifies first reconcile shows P4 (zero k2 history)
#   7. Launches the benchmark harness
#   8. Kills the harness after "Harness completed successfully" (skips slow report conversion)
#   9. Copies metrics from PVC and runs post-processing (table + graph)
#
# Requirements:
#   - kubectl / oc with a valid session (run `oc login` first if needed)
#   - llm-d-benchmark repo cloned at ./llm-d-benchmark with .venv set up
#     (run `make benchmark-setup BENCHMARK_NAMESPACE=<ns>` once)
#   - The two-variant WVA stack already deployed in NAMESPACE
#   - Python 3 with matplotlib, pyyaml (pip install -r hack/benchmark/requirements.txt)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── defaults ──────────────────────────────────────────────────────────────────
NAMESPACE=""
MODEL_ID="unsloth/Meta-Llama-3.1-8B"
BENCHMARK_SPEC="guides/two-variant-wva"
BENCHMARK_WORKLOAD="prefill_heavy.yaml"
GENERATE_TABLE=false

PRIMARY_DEPLOY="unsloth--1409d52c-a-3-1-8b-decode"
V2_DEPLOY="unsloth--1409d52c-a-3-1-8b-decode-v2"
WVA_DEPLOY="workload-variant-autoscaler-controller-manager"
WVA_LABEL="app.kubernetes.io/name=workload-variant-autoscaler"
PVC_ACCESS_POD="access-to-harness-data-workload-pvc"

LLMDBENCHMARK="$REPO_ROOT/llm-d-benchmark/.venv/bin/llmdbenchmark"
WVA_LOG="/tmp/wva-debug-run.log"

# ── argument parsing ──────────────────────────────────────────────────────────
while getopts "n:m:s:w:th" opt; do
    case $opt in
        n) NAMESPACE="$OPTARG" ;;
        m) MODEL_ID="$OPTARG" ;;
        s) BENCHMARK_SPEC="$OPTARG" ;;
        w) BENCHMARK_WORKLOAD="$OPTARG" ;;
        t) GENERATE_TABLE=true ;;
        h) grep "^#" "$0" | grep -v "^#!/" | sed 's/^# *//' ; exit 0 ;;
        *) echo "Unknown option -$OPTARG"; exit 1 ;;
    esac
done

if [ -z "$NAMESPACE" ]; then
    echo "ERROR: -n NAMESPACE is required"
    exit 1
fi

# ── helpers ───────────────────────────────────────────────────────────────────
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

wait_for_1_1() {
    local ns="$1"; shift
    log "Waiting for model deployments to reach 1/1..."
    until [ "$(kubectl get deployment -n "$ns" "$PRIMARY_DEPLOY" "$V2_DEPLOY" \
        -o jsonpath='{range .items[*]}{.spec.replicas}{" "}{.status.replicas}{" "}{.status.readyReplicas}{"\n"}{end}' 2>/dev/null \
        | grep -cv '^1 1 1$')" = "0" ]; do
        echo -n "."; sleep 5
    done
    echo " done"
}

# ── step 1: stop WVA ──────────────────────────────────────────────────────────
log "Step 1: Stopping WVA controller..."
kubectl scale deployment/"$WVA_DEPLOY" -n "$NAMESPACE" --replicas=0 2>/dev/null
kubectl wait pod -n "$NAMESPACE" -l "$WVA_LABEL" \
    --for=delete --timeout=180s 2>/dev/null && log "WVA pod deleted"

# ── step 2: scale model pods to 1/1 ──────────────────────────────────────────
log "Step 2: Scaling model pods to 1/1 (no WVA competition)..."
kubectl scale deployment -n "$NAMESPACE" "$PRIMARY_DEPLOY" "$V2_DEPLOY" --replicas=1 2>/dev/null
wait_for_1_1 "$NAMESPACE"

# ── step 3: start fresh WVA ───────────────────────────────────────────────────
log "Step 3: Starting fresh WVA controller..."
kubectl scale deployment/"$WVA_DEPLOY" -n "$NAMESPACE" --replicas=1 2>/dev/null
kubectl rollout status deployment/"$WVA_DEPLOY" -n "$NAMESPACE" --timeout=120s 2>/dev/null
NEW_POD=$(kubectl get pod -n "$NAMESPACE" -l "$WVA_LABEL" --no-headers 2>/dev/null \
    | grep Running | awk '{print $1}' | head -1)
log "New WVA pod: $NEW_POD"

# ── step 4: start in-flight log capture ──────────────────────────────────────
log "Step 4: Starting in-flight WVA log capture -> $WVA_LOG"
kubectl logs -n "$NAMESPACE" "$NEW_POD" -f --ignore-errors 2>/dev/null > "$WVA_LOG" &
LOG_PID=$!

# ── step 5: verify clean P4 history ──────────────────────────────────────────
log "Step 5: Waiting for first reconcile and verifying clean k2 history..."
sleep 35
K2_CHECK=$(kubectl logs -n "$NAMESPACE" "$NEW_POD" --tail=20 2>/dev/null \
    | grep "k2-source" | head -4)
if echo "$K2_CHECK" | grep -q '"priority": 4'; then
    log "VERIFIED: P4 (fallback-k1) — zero k2 history. Clean start confirmed."
else
    log "WARNING: Did not see P4 in first reconcile. k2 history may not be clean:"
    echo "$K2_CHECK"
    read -r -p "Continue anyway? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { kill $LOG_PID 2>/dev/null; exit 1; }
fi

# ── step 6: launch benchmark ─────────────────────────────────────────────────
log "Step 6: Launching benchmark (spec=$BENCHMARK_SPEC, workload=$BENCHMARK_WORKLOAD)..."

# Copy scenario to llm-d-benchmark workload profiles
SCENARIO_SRC="$REPO_ROOT/test/benchmark/scenarios/$BENCHMARK_WORKLOAD"
SCENARIO_DST="$REPO_ROOT/llm-d-benchmark/workload/profiles/guidellm/$BENCHMARK_WORKLOAD"
[ -f "$SCENARIO_SRC" ] && cp "$SCENARIO_SRC" "$SCENARIO_DST"

"$LLMDBENCHMARK" \
    --spec "$BENCHMARK_SPEC" \
    --workspace "$REPO_ROOT" \
    --base-dir "$REPO_ROOT/llm-d-benchmark" \
    run \
    -p "$NAMESPACE" \
    -l guidellm \
    -w "$BENCHMARK_WORKLOAD" \
    -m "$MODEL_ID" \
    --monitoring &
BENCH_PID=$!

# ── step 7: wait for harness completion and kill to skip slow conversion ───────
log "Step 7: Waiting for harness to complete benchmark + metrics collection..."
until HARNESS_POD=$(kubectl get pod -n "$NAMESPACE" -l app=llmdbench-harness-launcher \
    --no-headers 2>/dev/null | grep Running | awk '{print $1}' | head -1) \
    && [ -n "$HARNESS_POD" ]; do sleep 5; done
log "Harness pod: $HARNESS_POD"

until kubectl logs -n "$NAMESPACE" "$HARNESS_POD" --ignore-errors 2>/dev/null \
    | grep -q "Harness completed successfully"; do sleep 10; done

log "Harness completed. Killing pod to skip slow report conversion..."
kubectl delete pod -n "$NAMESPACE" "$HARNESS_POD" --grace-period=0 --force 2>/dev/null || true
wait "$BENCH_PID" 2>/dev/null || true
kill "$LOG_PID" 2>/dev/null || true

# ── step 8: copy metrics from PVC ─────────────────────────────────────────────
log "Step 8: Copying metrics from PVC..."
EXP_ID=$(kubectl exec -n "$NAMESPACE" "$PVC_ACCESS_POD" -- ls /requests/ 2>/dev/null | sort | tail -1)
WORKSPACE=$(ls -dt "$REPO_ROOT"/biran-* 2>/dev/null | head -1)
RESULTS="$WORKSPACE/results/$EXP_ID"
mkdir -p "$RESULTS"

kubectl cp "$NAMESPACE/$PVC_ACCESS_POD:/requests/$EXP_ID/metrics" "$RESULTS/" 2>/dev/null || true
mkdir -p "$RESULTS/metrics"
mv "$RESULTS/raw" "$RESULTS/graphs" "$RESULTS/processed" "$RESULTS/metrics/" 2>/dev/null || true

# Copy WVA log
cp "$WVA_LOG" "$RESULTS/wva-debug-run.log"
log "Results dir: $RESULTS  ($(ls "$RESULTS/metrics/raw/" 2>/dev/null | wc -l) raw files)"

# ── step 9: post-processing ───────────────────────────────────────────────────
log "Step 9: Running post-processing..."
cd "$REPO_ROOT"

if [ "$GENERATE_TABLE" = true ]; then
    log "  Generating per-reconcile decision table (requires debug image)..."
    python3 hack/benchmark/dump_wva_decision_table.py "$RESULTS"
fi

python3 hack/benchmark/dump_capacity_demand_estimate.py "$RESULTS"

# Generate wva_target_timeseries from local log
python3 - <<'PYEOF'
import json, re, sys
from pathlib import Path
from datetime import datetime, timezone

results = Path(sys.argv[1])
log_text = (results / "wva-debug-run.log").read_text()

RE_DEC = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+INFO.*Applied saturation decision'
    r'.*"variant":\s*"[^/]+/([^"]+)".*"action":\s*"([^"]+)".*"target":\s*(\d+)')
RE_ANA = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+INFO.*V2 saturation analysis completed'
    r'.*"totalSupply":\s*([\d.]+).*"totalDemand":\s*([\d.]+).*"utilization":\s*([\d.eE+\-]+)')

def epoch(ts):
    return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())

cur, slots = {"primary": 1, "v2": 1}, {}
for line in log_text.splitlines():
    m = RE_DEC.match(line)
    if m:
        ts, var, action, tgt = m.group(1), m.group(2), m.group(3), int(m.group(4))
        cur["v2" if var.endswith("-v2") else "primary"] = tgt
        s = slots.setdefault(ts, {}); s["ts"] = ts
        s["primary"] = cur["primary"]; s["v2"] = cur["v2"]
        continue
    m = RE_ANA.match(line)
    if m:
        ts = m.group(1); s = slots.setdefault(ts, {})
        s.setdefault("primary", cur["primary"]); s.setdefault("v2", cur["v2"])
        s["totalSupply"] = float(m.group(2)); s["totalDemand"] = float(m.group(3))
        s["utilization"] = float(m.group(4))

samples = [{"timestamp": epoch(ts), "primary": s.get("primary", 1), "v2": s.get("v2", 1),
            **{k: s[k] for k in ("totalSupply","totalDemand","utilization") if k in s}}
           for ts, s in sorted(slots.items())]
out = results / "metrics/processed/wva_target_timeseries.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"samples": samples}, indent=2))
print(f"Wrote {out} ({len(samples)} samples)")
PYEOF "$RESULTS"

python3 hack/benchmark/plot_two_variant_pipeline.py "$RESULTS"

log "Done. Results:"
[ "$GENERATE_TABLE" = true ] && log "  Table:  $RESULTS/metrics/processed/wva_decision_table.txt"
log "  Graph:  $RESULTS/metrics/graphs/two_variant_v2_full_pipeline.png"
if [ "$GENERATE_TABLE" = true ]; then
    echo ""
    log "To view the decision table:"
    echo "  cat $RESULTS/metrics/processed/wva_decision_table.txt"
else
    echo ""
    log "Tip: re-run with -t to also generate the per-reconcile decision table."
fi
