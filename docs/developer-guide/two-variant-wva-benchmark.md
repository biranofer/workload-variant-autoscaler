# Two-Variant WVA Benchmark

End-to-end guide for the **two-variant cost-aware scaling** benchmark: a single
model deployed as two variants of differing `variantCost` under one shared
`InferencePool` / EPP, used to exercise the WVA saturation V2 cost-aware
optimizer. The cheaper variant should absorb load before the expensive one
scales up.

For cluster login, namespace setup, and HuggingFace token configuration, follow
[`benchmark-guide.md`](benchmark-guide.md) Steps 1–4 first; this document picks
up after those.

---

## Topology

One `InferencePool` and one EPP front two `vLLM` `Deployment`s of the same
model. Each `Deployment` has its own `VariantAutoscaling` (VA) and `HPA`, but
both VAs share the same `spec.modelID` so the WVA saturation engine groups
them and applies cost-weighted scaling.

```
            +------------- Gateway --------------+
            |  HTTPRoute -> InferencePool (1 EPP)|
            +-------------------------------------+
                              |
               +--------------+--------------+
               |                             |
     +---------+--------+        +-----------+-----+
     | vLLM decode      |        | vLLM decode     |
     | primary (cost 10)|        | secondary (5)   |
     | VA + HPA         |        | VA + HPA        |
     +------------------+        +-----------------+
                   ^                       ^
                   +-------- WVA ----------+
                              controller
```

### Label strategy (how both Deployments share one pool)

The `InferencePool` EPP selects pods by two camelCase labels:

```
llm-d.ai/inferenceServing: "true"
llm-d.ai/model:            <model-hash>
```

The primary `Deployment` (managed by the `llm-d-modelservice` chart) adds a
third selector label, kebab-case: `llm-d.ai/inference-serving: "true"`.

The secondary `Deployment` created by `add_variant.py`:

- **Keeps** `llm-d.ai/inferenceServing` + `llm-d.ai/model` so the pool picks
  up its pods.
- **Omits** `llm-d.ai/inference-serving` so the primary `Deployment` does
  not claim secondary pods.
- **Adds** `wva.llmd.ai/variant: <suffix>` (default `v2`) as the secondary
  `Deployment`'s own selector discriminator.

Both VAs additionally carry a `llm-d.ai/variant: <va-name>` pod label so
Prometheus can map each scrape target back to its VA (see "Required
relabeling" below).

---

## Required pieces

The benchmark only works end-to-end when **all** of these are in place. They
are independent, and a mismatch on any one will silently degrade the run.

### 1. Workload-variant-autoscaler chart with the vllm ServiceMonitor

`charts/workload-variant-autoscaler/templates/vllm-servicemonitor.yaml` (gated
by `wva.vllmService.enabled: true` in the scenario yaml) installs a
`ServiceMonitor` that propagates the per-pod `llm-d.ai/variant` label into
scraped metrics as the Prometheus label `llm_d_ai_variant`. The V2 collector
uses that label to map metric rows to a VA. Without it, every reconcile prints
`No saturation metrics available for model, skipping analysis` and the
controller cannot scale.

The published chart at `oci://ghcr.io/llm-d/workload-variant-autoscaler:0.6.0`
**does not include this template**. There are two ways to satisfy the
requirement:

- **Install WVA from this branch's local chart** (`charts/workload-variant-autoscaler/`).
- **Patch the `PodMonitor`s in place** on a v0.6.0 install. Both PodMonitors
  scraping vLLM pods need this relabeling under each
  `spec.podMetricsEndpoints[*]`:

  ```yaml
  relabelings:
    - sourceLabels: [__meta_kubernetes_pod_label_llm_d_ai_variant]
      targetLabel: llm_d_ai_variant
      action: replace
  ```

  In a fresh standup the PodMonitors typically created are
  `unsloth--*-decode-podmonitor` (from the modelservice chart) and
  `vllm-unsloth--*` (broader selector). Both need the relabeling.

### 2. Saturation V2 enabled via configmap

The V1 saturation analyzer is the default. To select V2, the
`workload-variant-autoscaler-wva-saturation-scaling-config` ConfigMap's
`default` entry must set `analyzerName: saturation`:

```bash
kubectl apply -n $NS -f hack/benchmark/scenarios/wva_threshold/wva_saturation_v2_config.yaml
```

Verify in the controller log: each reconcile should print
`Processing model (V2)` (line 631 of `internal/engines/saturation/engine.go`).

### 3. (Recommended) `cache_config_info`-aware controller image

The V2 analyzer reads per-replica KV capacity from `vllm:cache_config_info`.
Two recent fixes on this branch are required for correct grouping when more
than one model lives in the cluster:

- `94ca8689 fix(collector): skip foreign-model pods in cache_config_info processing`
- `935605d7 fix(collector): drop model_name filter from cache_config_info query`

These are not in `v0.6.0`. Build and push from this branch, or use a
prebuilt image (e.g.
`ghcr.io/biranofer/llm-d-workload-variant-autoscaler:fix-cache-config-info`),
and patch the running controller:

```bash
kubectl set image -n $NS \
  deploy/workload-variant-autoscaler-controller-manager \
  manager=<your-registry>/llm-d-workload-variant-autoscaler:<tag>
kubectl rollout status -n $NS \
  deploy/workload-variant-autoscaler-controller-manager
```

Without these fixes the V2 analyzer falls back to
`computeReplicaCapacityFallback`, which uses the per-step compute budget
(`EffectiveMaxBatchedTokens`) instead of real KV memory. Observed effect on
Llama-3.1-8B / H100: capacity is under-stated by ~50×, so even moderate load
saturates the pool and the optimizer scales every variant to `maxReplicas`.

### 4. Newer vLLM image

The scenario yaml pins `docker.io/vllm/vllm-openai:v0.14.0`. This is required
because the default llm-d ships `v0.9.2` which does **not** emit
`vllm:cache_config_info` at all.

---

## How to run

Set `NS` to your namespace, then walk the steps below from the repo root.

### Step 0 — Place the scenario yaml inside the llm-d-benchmark checkout

`make benchmark-standup` invokes the upstream `llm-d-benchmark` CLI, which
expects the scenario file at
`<llm-d-benchmark-checkout>/config/scenarios/guides/two-variant-wva.yaml`.
That file is **not** part of upstream `llm-d-benchmark` — this repo carries
the canonical copy at
[`hack/benchmark/scenarios/guides/two-variant-wva.yaml`](../../hack/benchmark/scenarios/guides/two-variant-wva.yaml).
Copy it into place once after the first `make benchmark-standup` call (or
beforehand, if you've already cloned `llm-d-benchmark` manually):

```bash
mkdir -p llm-d-benchmark/config/scenarios/guides
cp hack/benchmark/scenarios/guides/two-variant-wva.yaml \
   llm-d-benchmark/config/scenarios/guides/two-variant-wva.yaml
```

If `llm-d-benchmark/` does not yet exist, the Makefile target
`benchmark-prereq` will clone it for you on first standup; re-run the copy
afterwards.

### Step 1 — Stand up the primary variant

```bash
make benchmark-standup BENCHMARK_NAMESPACE=$NS BENCHMARK_SPEC=guides/two-variant-wva
```

This installs the `llm-d-infra`, `inferencepool-gaie`, `modelservice`, and
`workload-variant-autoscaler` Helm releases for `unsloth/Meta-Llama-3.1-8B`
with `variantCost: "10.0"` and `min/maxReplicas: 1/10`.

### Step 2 — Add the secondary variant

```bash
python hack/benchmark/add_variant.py -n $NS                       # default cost 5.0, suffix v2
# or
python hack/benchmark/add_variant.py -n $NS \
    --variant-cost 3.0 --variant-suffix v3 \
    --min-replicas 1 --max-replicas 10
```

Verify both VAs and HPAs are present:

```bash
kubectl get va,hpa -n $NS
kubectl get pods -n $NS -l 'llm-d.ai/inferenceServing=true,llm-d.ai/model=unsloth--1409d52c-a-3-1-8b'
```

### Step 3 — Enable V2 saturation

```bash
kubectl apply -n $NS -f hack/benchmark/scenarios/wva_threshold/wva_saturation_v2_config.yaml
```

Confirm the analyzer switched:

```bash
kubectl logs -n $NS -l app.kubernetes.io/name=workload-variant-autoscaler \
  --tail=200 | grep "Processing model"
```

You want `Processing model (V2)`, not `(V1)`.

### Step 4 — (If using a v0.6.0 chart) patch PodMonitors and image

See [Required pieces #1 and #3](#required-pieces) above.

### Step 5 — (Optional) Tune HPA scale-up

The shipped HPA has `scaleUp.stabilizationWindowSeconds: 120`. If you want the
HPA to follow WVA decisions immediately rather than damping them:

```bash
for hpa in unsloth--1409d52c-a-3-1-8b-decode unsloth--1409d52c-a-3-1-8b-decode-v2; do
  kubectl patch hpa -n $NS "$hpa" --type=json \
    -p '[{"op":"replace","path":"/spec/behavior/scaleUp/stabilizationWindowSeconds","value":0}]'
done
```

Leaving `scaleDown` windowed at 120 s prevents flapping when a brief lull
arrives.

### Step 6 — Run the benchmark

The default workload is `test/benchmark/scenarios/prefill_heavy.yaml`. Edit
the file (`rate`, `max_seconds`, `prompt_tokens`, `output_tokens`) before
invoking — `make benchmark-run` copies the file at run-time, so the value on
disk at invocation is what gets used.

```bash
make benchmark-run BENCHMARK_NAMESPACE=$NS BENCHMARK_SPEC=guides/two-variant-wva
# Override workload:
make benchmark-run BENCHMARK_NAMESPACE=$NS BENCHMARK_SPEC=guides/two-variant-wva \
     BENCHMARK_WORKLOAD=symmetrical.yaml
```

Each run produces a workspace under `$REPO/biran-<timestamp>/...` with raw
metrics, logs, and processed timeseries.

### Step 7 — Teardown

```bash
make benchmark-teardown BENCHMARK_NAMESPACE=$NS
```

This removes the four Helm releases. The secondary variant created by
`add_variant.py` is plain Kubernetes objects (Deployment, VA, HPA) that the
modelservice chart's owner refs cover, so teardown removes them too.

---

## Generating the two-variant pipeline graph

`hack/benchmark/plot_two_variant_pipeline.py` produces a 5-panel timeseries
chart from a treatment results directory: replica count, KV cache utilization,
requests running, requests waiting, and EPP queue metrics — all split by
primary vs. secondary.

```bash
python3 hack/benchmark/plot_two_variant_pipeline.py \
  $REPO/biran-<timestamp>/results/guidellm-<id>_1 \
  --suffix "(rate=10, V2 enabled)"
```

Output goes to
`<results>/metrics/graphs/two_variant_v2_full_pipeline.png`.

The script reads:

- `metrics/raw/*.log` — Prometheus text dumps per pod per scrape time.
- `metrics/processed/replica_status_timeseries.json` — Deployment replica
  counts over time.

---

## Verifying cost-aware behavior

In the controller log during sustained load you should see, ordered by
priority:

1. The cheaper variant scaling up first.
2. The expensive variant joining only when the cheaper one's `maxReplicas`
   cannot absorb demand alone.
3. On scale-down, the expensive variant shrinking first.

Sample line (taken from a real run):

```
saturation/engine_v2.go:65   V2 saturation analysis completed
  modelID=unsloth/Meta-Llama-3.1-8B totalSupply=2503400 totalDemand=2229945
  utilization=0.89 requiredCapacity=120065 spareCapacity=0
```

`totalSupply` should track the realized capacity (sum of `cache_config_info`
across ready pods) — typical values for Llama-3.1-8B / H100 are ~315k tokens
per pod, so ~3M tokens at 10+0 replicas. If you see numbers near 13k, the
collector fell back to the per-step batch budget (see
[Required pieces #3](#required-pieces)).

---

## Files involved

| Path | Role |
|---|---|
| `hack/benchmark/scenarios/guides/two-variant-wva.yaml` | Scenario / values for primary stack (cost 10, min/max 1/10, HPA 100% per 15 s, vllmService enabled). **Must be copied into `llm-d-benchmark/config/scenarios/guides/` before standup** (see [Step 0](#step-0--place-the-scenario-yaml-inside-the-llm-d-benchmark-checkout)). |
| `hack/benchmark/add_variant.py` | Creates secondary `Deployment`/`VA`/`HPA` from primary, with the kebab-label trick |
| `hack/benchmark/scenarios/wva_threshold/wva_saturation_v2_config.yaml` | ConfigMap setting `analyzerName: saturation` to select V2 |
| `hack/benchmark/plot_two_variant_pipeline.py` | 5-panel pipeline graph generator |
| `charts/workload-variant-autoscaler/templates/vllm-servicemonitor.yaml` | `ServiceMonitor` that propagates `llm-d.ai/variant` label to Prometheus |
| `test/benchmark/scenarios/prefill_heavy.yaml` | Default workload for `make benchmark-run` |

---

## Tuning knobs

| Knob | Where | Effect |
|---|---|---|
| `scenario[0].wva.variantAutoscaling.variantCost` | `two-variant-wva.yaml` | Primary cost (default 10) |
| `--variant-cost` flag | `add_variant.py` | Secondary cost (default 5) |
| `--variant-suffix` flag | `add_variant.py` | Secondary `Deployment`/VA/HPA name suffix |
| `min/maxReplicas` | scenario yaml & flags | Per-variant scaling bounds |
| `HPA spec.behavior.scaleUp.stabilizationWindowSeconds` | live patch | 0 = follow WVA immediately; 120 = damp 2 min |
| `rate`, `max_seconds`, `prompt_tokens`, `output_tokens` | `prefill_heavy.yaml` | Workload shape |

---

## Common failure modes

- **`No saturation metrics available for model, skipping analysis` on every reconcile**
  → Variant label not propagated. Check that one of the PodMonitors scraping
  the vLLM pods has the `llm_d_ai_variant` relabeling
  ([Required pieces #1](#required-pieces)).
- **`Processing model (V1)` instead of `(V2)`**
  → Saturation configmap missing `analyzerName: saturation`. Apply
  `wva_saturation_v2_config.yaml` ([Step 3](#step-3--enable-v2-saturation)).
- **Both variants scale to `maxReplicas` immediately under modest load**
  → V2 read fallback capacity, not real KV. Check the controller image carries
  the `cache_config_info` fixes
  ([Required pieces #3](#required-pieces)) and that the model server image
  emits `vllm:cache_config_info` ([Required pieces #4](#required-pieces)).
- **Primary scales up while secondary still has headroom**
  → Demand exceeds what the cheaper variant alone can absorb at its
  `maxReplicas`. Raise the secondary's `--max-replicas` or lower the workload
  rate.
