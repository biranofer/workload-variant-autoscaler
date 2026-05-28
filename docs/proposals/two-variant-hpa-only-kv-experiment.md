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
