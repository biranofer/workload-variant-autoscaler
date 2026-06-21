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

The benchmark only works end-to-end when **all** of these are in place. The
scenario yaml at [`hack/benchmark/scenarios/guides/two-variant-wva.yaml`](../../hack/benchmark/scenarios/guides/two-variant-wva.yaml)
pins versions that satisfy these requirements out of the box.

### 1. WVA chart and controller image at v0.8.0-rc5 or newer

The published chart and controller image must include:

- The `vllm-servicemonitor.yaml` template (propagates the per-pod
  `llm-d.ai/variant` label into scraped metrics as `llm_d_ai_variant`, gated
  by `wva.vllmService.enabled: true`). Without this, every reconcile prints
  `No saturation metrics available for model, skipping analysis` and the
  controller cannot scale.
- The `cache_config_info` collector fixes (PR #1198) so the V2 analyzer
  groups per-replica KV capacity correctly when more than one model lives
  in the cluster.

Both landed in `v0.8.0-rc1`. The scenario yaml pins both `chartVersions.wva`
and `wva.image.tag` to `v0.8.0-rc5`. `v0.7.0` and earlier are missing one or
both fixes — do not downgrade.

### 2. Saturation V2 enabled via configmap

Saturation V2 is not the default in upstream (the default was reverted in
PR #1286). The benchmark flow enables it via [`make benchmark-enable-v2-saturation`](#step-3--enable-saturation-v2)
which applies the ConfigMap at
[`hack/benchmark/scenarios/wva_threshold/wva_saturation_v2_config.yaml`](../../hack/benchmark/scenarios/wva_threshold/wva_saturation_v2_config.yaml)
and restarts the controller. Each reconcile should then print
`Processing model (V2)` in the controller log.

### 3. Newer vLLM image

The scenario yaml pins `docker.io/vllm/vllm-openai:v0.14.0`. This is required
because the default llm-d ships `v0.9.2` which does **not** emit
`vllm:cache_config_info` at all.

---

## How to run

Set `NS` to your namespace, then walk the steps below from the repo root.

### Step 0 — Install the llm-d-benchmark CLI (one-time)

The make targets shell out to `llmdbenchmark` from a checkout of
[`llm-d-benchmark`](https://github.com/llm-d/llm-d-benchmark). On a fresh
clone of this repo:

```bash
make benchmark-install
```

Idempotent — re-running checks out the pinned `BENCHMARK_REPO_REF` and
re-installs the CLI.

### Cluster prerequisites — check before Step 1

The standup installs `prometheus-adapter` via Helm into
`openshift-user-workload-monitoring`. On clusters where `prometheus-adapter`
is **already** running but **not** as a Helm release (the new
Kustomize-based WVA install does this, as do plain `kubectl apply`
deployments), the install fails because the cluster-scoped APIService
`v1beta1.external.metrics.k8s.io` is owned by the non-Helm install.

Run all three of these checks. If the answers match what's shown, you must
pass `BENCHMARK_SKIP_PROMETHEUS_ADAPTER=true` to **every** `benchmark-standup`
invocation in Step 1:

```bash
helm list -A | grep prometheus-adapter                    # → empty
kubectl get apiservice v1beta1.external.metrics.k8s.io    # → exists
kubectl get clusterrole prometheus-adapter-resource-reader # → NotFound
```

The flag creates a stub `prometheus-adapter-resource-reader` ClusterRole
annotated with Helm release metadata, which makes `llmdbenchmark`'s
existing-PA probe pass and the conflicting Helm install is skipped. The
cluster's existing PA continues to serve `wva_desired_replicas` to your
HPAs. Override the release-namespace annotation with
`WVA_MONITORING_NAMESPACE=<ns>` if your PA lives somewhere other than
`workload-variant-autoscaler-monitoring`.

If all three checks return the *opposite* (no APIService, ClusterRole
exists with Helm annotations, or a Helm release shows up), skip the flag —
the standup will install or upgrade PA cleanly.

### Step 1 — Stand up the primary variant

```bash
make benchmark-standup BENCHMARK_NAMESPACE=$NS \
                       BENCHMARK_SPEC=guides/two-variant-wva \
                       BENCHMARK_MODEL_ID=unsloth/Meta-Llama-3.1-8B
```

`benchmark-standup` copies two files into the `llm-d-benchmark` checkout:

- [`hack/benchmark/scenarios/guides/two-variant-wva.yaml`](../../hack/benchmark/scenarios/guides/two-variant-wva.yaml)
  — the scenario values (variant costs, replicas, image pins, HPA behavior).
- [`hack/benchmark/scenarios/guides/two-variant-wva.yaml.j2`](../../hack/benchmark/scenarios/guides/two-variant-wva.yaml.j2)
  — the specification wrapper that the `--spec` flag actually loads.

Standup then installs the `llm-d-infra`, `inferencepool-gaie`,
`modelservice`, and `workload-variant-autoscaler` Helm releases for the
chosen model with `variantCost: "10.0"`, `min/maxReplicas: 1/10`, and primary
`tensor: 2`. `BENCHMARK_MODEL_ID` is required — without it the standup
defaults to a placeholder dummy model.

### Step 2 — Add the secondary variant

```bash
make benchmark-add-variant BENCHMARK_NAMESPACE=$NS
```

This invokes `hack/benchmark/add_variant.py` against the variant config at
`hack/benchmark/scenarios/guides/variants/v2-tp1-cheaper.yaml` (default —
override with `VARIANT_CONFIG=<path>`), creating a secondary `Deployment`,
`VariantAutoscaling`, and `HPA` named with the `v2` suffix and
`variantCost: "5.0"`.

Verify both VAs and HPAs are present:

```bash
kubectl get va,hpa -n $NS
kubectl get pods -n $NS -l 'llm-d.ai/inferenceServing=true,llm-d.ai/model=unsloth--1409d52c-a-3-1-8b'
```

### Step 3 — Enable saturation V2

Saturation V2 is not the default upstream. Apply the ConfigMap and restart
the controller:

```bash
make benchmark-enable-v2-saturation BENCHMARK_NAMESPACE=$NS
```

Confirm the analyzer switched:

```bash
kubectl logs -n $NS -l app.kubernetes.io/name=workload-variant-autoscaler \
  --tail=200 | grep "Processing model"
```

You want `Processing model (V2)`, not `(V1)`.

### Step 4 — (Optional) Tune HPA scale-up

The shipped HPA has `scaleUp.stabilizationWindowSeconds: 120`. If you want
the HPA to follow WVA decisions immediately rather than damping them:

```bash
for hpa in unsloth--1409d52c-a-3-1-8b-decode unsloth--1409d52c-a-3-1-8b-decode-v2; do
  kubectl patch hpa -n $NS "$hpa" --type=json \
    -p '[{"op":"replace","path":"/spec/behavior/scaleUp/stabilizationWindowSeconds","value":0}]'
done
```

Leaving `scaleDown` windowed at 120 s prevents flapping when a brief lull
arrives.

### Step 5 — Run the benchmark

The default workload is `test/benchmark/scenarios/prefill_heavy.yaml.in`.
Edit the file (`rate`, `max_seconds`, `prompt_tokens`, `output_tokens`)
before invoking — `make benchmark-run` copies the file at run-time, so the
value on disk at invocation is what gets used.

For multi-run comparisons, restart the controller between runs to flush k2
history (otherwise stale per-replica capacity estimates from the previous
run can poison the next):

```bash
make benchmark-restart-controller BENCHMARK_NAMESPACE=$NS
make benchmark-run BENCHMARK_NAMESPACE=$NS BENCHMARK_SPEC=guides/two-variant-wva
# Override workload:
make benchmark-run BENCHMARK_NAMESPACE=$NS BENCHMARK_SPEC=guides/two-variant-wva \
     BENCHMARK_WORKLOAD=symmetrical.yaml
```

Each run produces a workspace under `$REPO/biran-<timestamp>/...` with raw
metrics, logs, and processed timeseries.

### Step 6 — Teardown

```bash
make benchmark-teardown BENCHMARK_NAMESPACE=$NS \
                        BENCHMARK_SPEC=guides/two-variant-wva
```

`BENCHMARK_SPEC` must be passed (the make target's default is the
`guides/workload-autoscaling` scenario, which the CLI can't find for
two-variant teardown).

This removes the four Helm releases and the secondary variant. The
secondary `Deployment` is created by `benchmark-add-variant` outside any
Helm release; the llm-d-benchmark teardown explicitly deletes orphaned
Deployments in the namespace, and `add_variant.py` sets `ownerReferences`
on the secondary `VariantAutoscaling` and `HPA` pointing at the secondary
`Deployment` so they cascade-delete with it.

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
collector fell back to the per-step batch budget — verify the chart and
controller image are both at `v0.8.0-rc5` or newer
([Required pieces #1](#1-wva-chart-and-controller-image-at-v080-rc5-or-newer)).

---

## Files involved

| Path | Role |
|---|---|
| `hack/benchmark/scenarios/guides/two-variant-wva.yaml` | Scenario / values for primary stack (cost 10, min/max 1/10, TP=2, HPA 100% per 15 s, vllmService enabled). Copied into the `llm-d-benchmark` checkout automatically by `make benchmark-standup`. |
| `hack/benchmark/scenarios/guides/variants/v2-tp1-cheaper.yaml` | Default secondary-variant config (suffix `v2`, cost 5.0, TP=1) consumed by `make benchmark-add-variant`. Override path with `VARIANT_CONFIG=<path>`. |
| `hack/benchmark/add_variant.py` | Creates secondary `Deployment`/`VA`/`HPA` from primary, with the kebab-label trick. |
| `hack/benchmark/scenarios/wva_threshold/wva_saturation_v2_config.yaml` | ConfigMap setting `analyzerName: saturation` to select V2. Applied by `make benchmark-enable-v2-saturation`. |
| `test/benchmark/scenarios/prefill_heavy.yaml.in` | Default workload for `make benchmark-run`. |

---

## Tuning knobs

| Knob | Where | Effect |
|---|---|---|
| `scenario[0].wva.variantAutoscaling.variantCost` | `two-variant-wva.yaml` | Primary cost (default 10) |
| `variantCost` field | `variants/v2-tp1-cheaper.yaml` (or other `VARIANT_CONFIG`) | Secondary cost (default 5) |
| `suffix` field | variant config yaml | Secondary `Deployment`/VA/HPA name suffix (default `v2`) |
| `minReplicas` / `maxReplicas` | scenario yaml & variant config | Per-variant scaling bounds |
| `HPA spec.behavior.scaleUp.stabilizationWindowSeconds` | live patch | 0 = follow WVA immediately; 120 = damp 2 min |
| `rate`, `max_seconds`, `prompt_tokens`, `output_tokens` | `prefill_heavy.yaml.in` | Workload shape |

---

## Common failure modes

- **`No saturation metrics available for model, skipping analysis` on every reconcile**
  → Variant label not propagated. Verify the chart pinned in the scenario
  yaml is `v0.8.0-rc5` or newer
  ([Required pieces #1](#1-wva-chart-and-controller-image-at-v080-rc5-or-newer)).
- **`Processing model (V1)` instead of `(V2)`**
  → Saturation configmap missing `analyzerName: saturation`. Run
  `make benchmark-enable-v2-saturation BENCHMARK_NAMESPACE=$NS`
  ([Step 3](#step-3--enable-saturation-v2)).
- **Both variants scale to `maxReplicas` immediately under modest load**
  → V2 read fallback capacity, not real KV. Verify the controller image
  pinned in the scenario yaml is `v0.8.0-rc5` or newer (carries the
  `cache_config_info` collector fixes from PR #1198), and that the model
  server image emits `vllm:cache_config_info`
  ([Required pieces #3](#3-newer-vllm-image)).
- **Primary scales up while secondary still has headroom**
  → Demand exceeds what the cheaper variant alone can absorb at its
  `maxReplicas`. Raise the secondary's `maxReplicas` in the variant config
  or lower the workload rate.
- **Stale capacity estimates after a previous run**
  → k2 history persists for the controller's lifetime. Run
  `make benchmark-restart-controller BENCHMARK_NAMESPACE=$NS` between runs
  to flush it ([Step 5](#step-5--run-the-benchmark)).
- **Standup fails at `[03] workload_monitoring` with**
  `APIService "v1beta1.external.metrics.k8s.io" exists and cannot be imported into the current release`
  → You skipped the [Cluster prerequisites](#cluster-prerequisites--check-before-step-1)
  check. Re-run standup with `BENCHMARK_SKIP_PROMETHEUS_ADAPTER=true`. The
  proper long-term fix is migrating the scenario yaml to the Kustomize-based
  WVA install path (tracked as follow-up to the Helm → Kustomize migration).
