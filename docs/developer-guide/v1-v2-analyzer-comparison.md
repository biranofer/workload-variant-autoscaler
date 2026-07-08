# V1 vs V2 Saturation Analyzer Comparison

This guide describes how to run and analyze a controlled experiment that
demonstrates the scaling-speed advantage of the V2 (token-based) saturation
analyzer over the V1 (percentage-based) analyzer on a single-variant deployment.

## Background

Both analyzers drive the WVA controller's scale-out decisions on a single variant
(primary TP=2):

- **V1 (`analyzerName` unset)**: percentage-based KV cache and queue thresholds;
  scales incrementally, one replica at a time.
- **V2 (`analyzerName: saturation`)**: token-based demand estimation (KV
  occupancy + vLLM wait queue + EPP queue); can jump to N replicas in a single
  reconcile when demand materially exceeds capacity.

Under a rising load ramp, V1 reacts too slowly — replicas arrive one by one while
requests queue up and TTFT/TPOT degrade. V2 converges much faster because it
calculates how many replicas the current demand requires rather than reacting to a
percentage threshold.

## Experiment Design

| Parameter       | Value                                |
|-----------------|--------------------------------------|
| Harness         | inference-perf                       |
| Scenario        | `prefill_rampup.yaml` (4000 in / 1000 out tokens, Poisson) |
| Rate stages     | 5 RPS × 5 min → 10 RPS × 5 min → 15 RPS × 5 min |
| Total duration  | 15 min per run                       |
| Variant         | Primary only (TP=2, cost=10)         |
| Runs            | 2 — one V1, one V2                   |

## Prerequisites

- Single-variant stack already stood up (`make benchmark-standup`).
- `llmdbenchmark` CLI installed (`make benchmark-install`).
- `post_run_analyze.sh` available in `hack/benchmark/`.

## Run Procedure

```bash
NS=<your-namespace>

# --- Before EVERY run: bounce decode pod + restart controller ---
# Flushes residual KV cache and controller in-memory state so each run
# starts with no history from the previous one.
_clean_start() {
    kubectl scale deploy -n $NS -l app.kubernetes.io/component=decode --replicas=0
    kubectl wait --for=delete pod -n $NS -l app.kubernetes.io/component=decode --timeout=120s
    kubectl scale deploy -n $NS -l app.kubernetes.io/component=decode --replicas=1
    local decode_deploy=$(kubectl get deploy -n $NS -l app.kubernetes.io/component=decode -o name | head -1 | cut -d/ -f2)
    kubectl rollout status -n $NS deploy/$decode_deploy --timeout=300s
    make benchmark-restart-controller BENCHMARK_NAMESPACE=$NS
    sleep 30  # one WVA reconcile to confirm KV=0%
}

# V1 run — ensure V1 analyzer is active (no analyzerName in configmap)
_clean_start
make benchmark-run BENCHMARK_NAMESPACE=$NS \
    BENCHMARK_HARNESS=inference-perf \
    BENCHMARK_WORKLOAD=prefill_rampup.yaml

# Run post-analysis immediately (WVA log buffer rotates)
V1_RESULTS="$(ls -td $PWD/$USER-*/results/inference-perf-*_1 2>/dev/null | head -1)"
./hack/benchmark/post_run_analyze.sh "$V1_RESULTS" $NS

# Switch to V2 and clean start
make benchmark-enable-v2-saturation BENCHMARK_NAMESPACE=$NS
_clean_start

# V2 run
make benchmark-run BENCHMARK_NAMESPACE=$NS \
    BENCHMARK_HARNESS=inference-perf \
    BENCHMARK_WORKLOAD=prefill_rampup.yaml

V2_RESULTS="$(ls -td $PWD/$USER-*/results/inference-perf-*_2 2>/dev/null | head -1)"
./hack/benchmark/post_run_analyze.sh "$V2_RESULTS" $NS
```

> **Verify analyzer**: before each run, check controller logs with
> `kubectl logs -n $NS deploy/workload-variant-autoscaler-controller-manager | grep "Processing model"`.
> V1 logs `Processing model (V1)`, V2 logs `Processing model (V2)`.

## Generating the Comparison

### Timeseries plot

```bash
python3 hack/benchmark/plot_v1_v2_comparison.py \
    --v1 "$V1_RESULTS" \
    --v2 "$V2_RESULTS" \
    --output comparison/
```

Output: `comparison/v1_v2_comparison.png` — 3 panels (replica count, KV cache
utilization, queue depth), each overlaying V1 (dashed blue) and V2 (solid red).
Vertical dotted lines mark the 5→10 RPS and 10→15 RPS transitions.

### Summary table

```bash
python3 hack/benchmark/postprocess.py \
    --labels "V1 Analyzer" "V2 Analyzer" \
    --gpus-per-replica 2 \
    --workload-duration-min 15 \
    "$V1_RESULTS" "$V2_RESULTS"
```

This produces a side-by-side markdown table including:

| Metric                | V1 Analyzer | V2 Analyzer |
|-----------------------|-------------|-------------|
| Avg TTFT (ms)         | ...         | ...         |
| P99 TTFT (ms)         | ...         | ...         |
| Avg TPOT (ms/token)   | ...         | ...         |
| P99 ITL (ms/token)    | ...         | ...         |
| Avg replicas          | ...         | ...         |
| Max replicas          | ...         | ...         |
| GPU time (GPU·min)    | ...         | ...         |
| Avg KV cache util     | ...         | ...         |
| Error count           | ...         | ...         |

**GPU time** = `avg_replicas × 2 GPUs × run_duration_min`. A lower GPU·min with
better SLO metrics is the ideal V2 outcome — it scales fast, reaches equilibrium,
and does not hold unnecessary replicas.

## Expected Outcomes

At rates ≥ 10 RPS:

- **V1**: slow stair-step replica growth; KV cache and queue stay elevated for
  several minutes; aggregate P99 TTFT significantly higher because requests queued
  during the ramp-up window pull the tail latency up.
- **V2**: rapid scale-out at each rate step; KV cache and queue return to
  baseline within 1–2 reconcile cycles; aggregate TTFT/TPOT materially lower.

At 5 RPS the load is typically below the saturation threshold for a single TP=2
replica, so both analyzers may show identical behavior.

## Resetting After the Experiment

To restore the default V1 analyzer for subsequent single-variant runs, restart
the controller without applying the V2 configmap:

```bash
make benchmark-restart-controller BENCHMARK_NAMESPACE=$NS
```

The controller will read the unmodified ConfigMap (no `analyzerName` field) and
revert to V1.
