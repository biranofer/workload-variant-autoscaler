# Proposal: Two-Variant HPA-only KV-utilization Experiment

**Status:** Draft for review &nbsp;·&nbsp; **Created:** 2026-05-29

---

## Problem

The two-variant benchmark documented in
[`two-variant-wva-benchmark.md`](../developer-guide/two-variant-wva-benchmark.md)
always relies on the WVA controller to compute desired replica counts — each
HPA tracks the `wva_desired_replicas` external metric 1:1. This makes it hard
to attribute observed gains (or regressions) to WVA's cost-aware optimizer
rather than to the underlying HPA scaling response. We need a like-for-like
comparison run where:

- the model server / EPP / gateway plumbing is identical to the WVA
  benchmark,
- WVA does **not** decide replica counts,
- each variant's HPA scales directly on its own pods'
  `vllm:kv_cache_usage_perc`, with target `0.8` and
  `scaleUp.stabilizationWindowSeconds=120`.

---

## Proposal

Add a converter script that flips a deployed `guides/two-variant-wva`
environment between WVA mode and HPA-only-KV mode without re-standup:

1. **Disable WVA** — `kubectl scale -n $NS
   deploy/workload-variant-autoscaler-controller-manager --replicas=0`.
   The controller stops emitting `wva_desired_replicas`. Existing VAs and
   the saturation ConfigMap are left in place (harmless when the controller
   is down).

2. **Add a KV Pods rule to prometheus-adapter** so HPAs can read it as a
   per-pod custom metric:

   ```yaml
   rules:
     custom:
       - seriesQuery: 'vllm:kv_cache_usage_perc{namespace!="",pod!=""}'
         resources:
           overrides:
             namespace: { resource: "namespace" }
             pod:       { resource: "pod" }
         name:
           matches: "^vllm:kv_cache_usage_perc$"
           as:      "vllm_kv_cache_usage_perc"
         metricsQuery: 'avg_over_time(<<.Series>>{<<.LabelMatchers>>}[1m])'
   ```

   Applied via `helm upgrade prometheus-adapter --reuse-values --values
   <rule-file>`. The `pod`/`namespace` labels are injected by the
   prometheus-operator scrape pipeline, so no additional relabeling is
   required.

3. **Replace both HPAs** (primary + secondary) with the new metric source:

   ```yaml
   spec:
     scaleTargetRef:
       kind: Deployment
       name: <existing>
     minReplicas: 1
     maxReplicas: 10
     metrics:
       - type: Pods
         pods:
           metric:
             name: vllm_kv_cache_usage_perc
           target:
             type: AverageValue
             averageValue: "800m"        # 0.8
     behavior:
       scaleUp:
         stabilizationWindowSeconds: 120
         selectPolicy: Max
         policies:
           - type: Percent
             value: 100
             periodSeconds: 15
       scaleDown:
         stabilizationWindowSeconds: 120
         selectPolicy: Max
         policies:
           - type: Percent
             value: 100
             periodSeconds: 15
   ```

The script saves the original HPA spec to a `hpa-pre-kv-snapshot` ConfigMap
before patching so that `--revert` restores the prior state byte-for-byte
(WVA mode, original HPA, dropped KV rule).

---

## Why a converter script (and not a new scenario yaml)

**Alternative considered (rejected):** add `guides/two-variant-hpa-kv` plus
new jinja templates (`21_prometheus-adapter-values-hpa-kv.yaml.j2`,
`28_hpa-kv.yaml.j2`) that render when `wva.enabled: false`.

| | Converter (recommended) | New scenario yaml |
|---|---|---|
| Lines added | ~150 (one script + one rule yaml) | ~350+ in this repo, plus 2 new jinja templates in upstream `llm-d-benchmark` we don't own |
| A/B between modes | one command, no re-standup | requires teardown + re-standup of full stack |
| Drift risk vs. WVA setup | none — plumbing is literally the same | new scenario can drift from the WVA scenario over time |
| Reproducibility on user fork | self-contained on our branch | requires fork + commit on `llm-d-benchmark` too |

The plumbing differs only in the scaling brain — adding a parallel scenario
duplicates everything that isn't different.

---

## Reversibility

Required so a single cluster supports back-to-back A/B runs:

```bash
python hack/benchmark/switch_to_hpa_only_kv.py -n $NS            # WVA -> HPA-only-KV
make benchmark-run BENCHMARK_NAMESPACE=$NS BENCHMARK_SPEC=guides/two-variant-wva
python hack/benchmark/switch_to_hpa_only_kv.py -n $NS --revert   # HPA-only-KV -> WVA
make benchmark-run BENCHMARK_NAMESPACE=$NS BENCHMARK_SPEC=guides/two-variant-wva
```

`--revert` scales WVA back to 1, removes the KV rule via
`helm upgrade --reuse-values --set 'rules.custom=null'`, and restores both
HPAs from the snapshot ConfigMap.

---

## Verification

1. WVA off — `kubectl get deploy -n $NS
   workload-variant-autoscaler-controller-manager
   -o jsonpath='{.spec.replicas}'` returns `0`.
2. Custom-metrics API exposes the rule —
   `kubectl get --raw
   "/apis/custom.metrics.k8s.io/v1beta1/namespaces/$NS/pods/*/vllm_kv_cache_usage_perc"`
   returns one entry per decode pod with a numeric value.
3. HPAs read it — `kubectl describe hpa -n $NS unsloth--*-decode` shows
   `Metrics: ( current / target ): "vllm_kv_cache_usage_perc" on pods:
   <X>m / 800m`, and `Conditions: ScalingActive=True`.
4. Behavior reflects the new windows — scaleUp `120s`, scaleDown `120s`.
5. End-to-end — during a benchmark run, both HPAs scale independently on
   their own variant's KV; no `wva_desired_replicas` reads.
6. Revert — after `--revert`, HPAs read `wva_desired_replicas` again, WVA
   is at `replicas=1`, and the KV rule is gone from the adapter ConfigMap.

---

## Open questions for team review

1. **`scaleDown` window** — proposal keeps it at `120 s`. Some teams prefer
   `240 s` to damp lull-driven thrashing. Worth changing in lockstep with
   scaleUp, or leave at 120?
2. **Controller image** — the running controller is on the
   `fix-cache-config-info` image. When WVA is scaled to 0 the image is
   inert. Should we leave it (`--revert` is a single step) or roll back to
   `v0.6.0` for HPA-only mode for cleanliness?
3. **HPA target** — `800m` (0.8) is the user's stated trigger. Some setups
   prefer `0.7` to leave headroom for spike absorption before scale-up. Do
   we want to standardize on `0.8` across both this experiment and any
   future HPA-on-KV scenarios?
4. **Approach** — converter (recommended) vs. parallel scenario yaml. Does
   anyone on the team want the orthogonal scenario yaml for clarity, even
   at the cost of duplicating ~350 lines and adding files to the upstream
   `llm-d-benchmark` repo?

---

## Implementation artifacts (out of scope for this proposal)

To be added in a follow-up implementation turn after this proposal is
approved:

- `hack/benchmark/switch_to_hpa_only_kv.py` — converter, with `--revert`.
- `hack/benchmark/scenarios/guides/hpa-kv-prometheus-adapter-rule.yaml` —
  the rule snippet that the script applies via `helm upgrade --reuse-values
  --values`.

---

## Addendum — finding from initial cluster exploration

Discovered after this proposal was first written, while inspecting the
cluster on 2026-05-29. Recorded here so the team can factor it in during
review.

The cluster has **two prometheus-adapter installs**, not one:

- `openshift-user-workload-monitoring/prometheus-adapter` (provided by the
  OpenShift cluster) — serves `v1beta1.custom.metrics.k8s.io` (the Pods /
  Object metrics path).
- `workload-variant-autoscaler-monitoring/prometheus-adapter` (installed by
  the WVA standup) — serves `v1beta1.external.metrics.k8s.io` (the External
  metrics path used today by the `wva_desired_replicas` HPA).

`v1beta1.custom.metrics.k8s.io` is bound to the OpenShift adapter cluster-wide
and is not configurable from this repo.

### Implication for the spec

The "Proposal" section above shows the rule under `rules.custom` and the HPA
metric block as `type: Pods`. **That would target the OpenShift-managed
adapter we cannot modify.** The implementation should instead extend the
WVA-installed External adapter with an External rule scoped per variant via
the existing `llm_d_ai_variant` label:

```yaml
rules:
  external:
    - seriesQuery: 'vllm:kv_cache_usage_perc{llm_d_ai_variant!=""}'
      resources:
        overrides:
          namespace: { resource: "namespace" }
      name:
        matches: "^vllm:kv_cache_usage_perc$"
        as:      "vllm_kv_cache_usage_perc"
      metricsQuery: 'avg(<<.Series>>{<<.LabelMatchers>>}) by (llm_d_ai_variant, namespace)'
```

And each HPA reads it as External with a per-variant selector:

```yaml
metrics:
  - type: External
    external:
      metric:
        name: vllm_kv_cache_usage_perc
        selector:
          matchLabels:
            llm_d_ai_variant: <va-name>      # primary or secondary
      target:
        type: Value
        value: "800m"                         # 0.8
```

This is a one-line spec change — same metric, same threshold, same behavior
windows — but routed through the External path that the WVA setup already
owns. No conflict with OpenShift's cluster-wide adapter, no API service
re-registration.

The `llm_d_ai_variant` label is already present on every vLLM scrape thanks
to the relabeling we added when enabling V2 saturation; no additional
PodMonitor work is required.

---

## Update — pivot to the upstream HPA-EPP pattern (2026-05-30)

Recorded after a discussion with the team about what HPA-only baseline to
compare WVA against. The proposal as originally written invented per-pod KV
utilization as the HPA signal. The llm-d project already publishes a
canonical HPA-only autoscaling pattern at
[`llm-d/llm-d/guides/workload-autoscaling/README.hpa-epp.md`](https://github.com/llm-d/llm-d/tree/main/guides/workload-autoscaling).
Comparing WVA against *that* pattern is a stronger, more defensible demo
than comparing against a homegrown KV-threshold scheme.

### Signals the upstream HPA-EPP pattern uses

Two model-level metrics emitted by the EPP, exposed via prometheus-adapter
as External metrics:

| Adapter `name.as` | Promql `metricsQuery` | HPA target |
|---|---|---|
| `epp_queue_size`       | `sum(inference_extension_flow_control_queue_size{inference_pool="<pool>"})` | `Value: "250"` |
| `epp_running_requests` | `sum(inference_objective_running_requests{top_level_controller_name="<epp>"})` | `AverageValue: "250"` |

Both are *model-level* sums — they have no per-variant attribution. WVA's V2
analyzer already reads `inference_extension_flow_control_queue_size` (and
`_queue_bytes`) for the same purpose (see
`internal/collector/registration/saturation.go:108-126`). Same gateway-side
signal, different reasoning layer.

HPA behavior in the upstream guide: `scaleUp.stabilizationWindowSeconds: 0`,
`scaleDown.stabilizationWindowSeconds: 300`, both `Percent: 100 / 15s`.

### Concrete spec replacement

**Adapter rules** (replaces the KV rule above; both rules apply to the
WVA-installed external adapter):

```yaml
rules:
  external:
    - seriesQuery: 'inference_extension_flow_control_queue_size'
      resources:
        overrides:
          namespace: { resource: "namespace" }
      name:
        matches: '^inference_extension_flow_control_queue_size$'
        as:      'epp_queue_size'
      metricsQuery: 'sum(inference_extension_flow_control_queue_size{inference_pool="unsloth--1409d52c-a-3-1-8b-gaie"})'

    - seriesQuery: 'inference_objective_running_requests'
      resources:
        overrides:
          namespace: { resource: "namespace" }
      name:
        matches: '^inference_objective_running_requests$'
        as:      'epp_running_requests'
      metricsQuery: 'sum(inference_objective_running_requests{top_level_controller_name="unsloth--1409d52c-a-3-1-8b-gaie-epp"})'
```

**HPA** for each variant Deployment (primary and secondary) — identical
metric block, only `scaleTargetRef.name` differs:

```yaml
metrics:
  - type: External
    external:
      metric: { name: epp_queue_size }
      target: { type: Value, value: "250" }
  - type: External
    external:
      metric: { name: epp_running_requests }
      target: { type: AverageValue, averageValue: "250" }

behavior:
  scaleUp:
    stabilizationWindowSeconds: 0
    policies: [{ type: Percent, value: 100, periodSeconds: 15 }]
  scaleDown:
    stabilizationWindowSeconds: 300
    policies: [{ type: Percent, value: 100, periodSeconds: 15 }]
```

### Threshold tuning — what 250 / 250 means and when to change it

The two `250`s are **literal threshold values**, not magic constants. They
control how aggressively the HPA scales:

- **`epp_queue_size = 250`** with `target.type: Value` — HPA scales to keep
  the *total* model-level gateway queue at ≈250 requests. If the queue
  climbs to 500, HPA wants ~2× the current replicas; if it drops to 250,
  HPA holds.
- **`epp_running_requests = 250`** with `target.type: AverageValue` — HPA
  divides the metric value by *its own* current replica count. With 1
  replica it targets 250 in-flight per pod; with 4 replicas it targets 250
  per pod, so the model can carry 1000 concurrent before scale-up.

The upstream chose `250 / 250` for Llama-3.1-8B on a 1-3 replica range.
Our setup uses Llama-3.1-8B too, but with a 1-10 replica range and
different load shapes. **Before running the comparison we need to verify
that HPA actually engages under the chosen workload rate**, otherwise
"WVA scales / HPA does not scale" is uninformative.

A practical first-cut for our `prefill_heavy.yaml` at rate=5: lower
`epp_queue_size` to ~`50–100` and keep `epp_running_requests` at `250` for
the smoke test. In our prior WVA run at rate=5 the EPP queue peaked around
~150, so a `250` threshold would never fire on that signal. Tune to ensure
HPA-EPP is *active* during the run.

The absolute thresholds do **not** need to match WVA's saturation config
values (`kvCacheThreshold: 0.8`, `queueLengthThreshold: 5`) for the
comparison to be fair — those govern WVA's internal logic, not HPA's. What
matters is that both modes operate on the same **gateway-side signal** and
both are actually allowed to scale during the run.

### Two HPAs per pool — the structural multi-variant gap

`HPA.spec.scaleTargetRef` points to a **single workload** — one Deployment,
one StatefulSet, one LeaderWorkerSet. It cannot scale two Deployments
coordinatedly. So with two variant Deployments we install **two HPAs**:

```yaml
# HPA #1
spec:
  scaleTargetRef: { kind: Deployment, name: unsloth--1409d52c-a-3-1-8b-decode }       # primary
  metrics: [ epp_queue_size: 250, epp_running_requests: 250 ]

# HPA #2
spec:
  scaleTargetRef: { kind: Deployment, name: unsloth--1409d52c-a-3-1-8b-decode-v2 }    # v2
  metrics: [ epp_queue_size: 250, epp_running_requests: 250 ]   # SAME metrics
```

Both Deployments belong to the same `InferencePool` / EPP, so both rules
return the same value at any instant. Two distinct HPAs, identical
observation, independent decisions. This produces two structural failure
modes that WVA explicitly fixes:

1. **Both scale up together.** Queue at 500 → both HPAs want ~2× replicas
   → primary AND v2 both add pods, even though scaling v2 alone (the
   cheaper variant) would have sufficed. Cost-blind reaction.
2. **`AverageValue` divides by per-Deployment replica count, not pool
   count.** If primary=1 and v2=5, primary's HPA divides the running-count
   by 1 and reacts immediately; v2's HPA divides by 5 and barely moves.
   Net effect: the **expensive variant scales sooner**, the opposite of
   what cost-aware reasoning wants.

Both pathologies are direct consequences of HPA's per-Deployment scope.
WVA reads the same signals at the *model* level and applies a cost-aware
optimizer across variants — exactly what neither HPA can do alone.

### What this changes in this proposal

- The "Proposal" section's `Pods` metric on `vllm:kv_cache_usage_perc` is
  **superseded** by the EPP-External metrics above for the comparison run.
- The implementation artifacts (`switch_to_hpa_only_kv.py` and the
  prometheus-adapter rule snippet) need updating to install the two
  EPP-based rules instead of one KV-based rule, and to apply the
  `epp_queue_size`/`epp_running_requests` HPA spec to both variant HPAs
  rather than a `vllm_kv_cache_usage_perc` block.
- The proposal filename retains `-kv-` for stability of any link already
  shared, but the experiment is now an "HPA-EPP vs. WVA" comparison
  matching upstream pattern naming.

### Open questions added by this update

5. **Threshold values** — start with upstream `250 / 250`, run a smoke at
   rate=5 to confirm HPA fires, then tune down? Or pick a deliberately
   lower starting point (e.g. `50 / 100`)?
6. **`scaleDown` window** — upstream uses `300 s`; our prior runs used
   `120 s`. Match upstream for fidelity, or keep `120 s` for parity with
   our WVA-mode behavior?
7. **`epp_running_requests` semantics** — `AverageValue` divides by *each
   HPA's* replica count, biasing scale-up toward the variant with fewer
   current replicas. Treat this as a known limitation of the upstream
   pattern (it is) and document it, or attempt to mitigate with a custom
   metric query?
