# Proposal: Two-Variant HPA-only Experiment (HPA-EPP baseline)

**Status:** Draft for review &nbsp;·&nbsp; **Created:** 2026-05-29 &nbsp;·&nbsp;
**Last updated:** 2026-05-31

---

## Problem

The two-variant benchmark documented in
[`two-variant-wva-benchmark.md`](../developer-guide/two-variant-wva-benchmark.md)
always relies on the WVA controller to compute desired replica counts.
Each HPA tracks the `wva_desired_replicas` external metric 1:1, so the
benchmark cannot tell us how much of any observed gain (or regression) is
attributable to WVA's cost-aware cross-variant optimizer versus the
underlying scaling response.

We need a like-for-like comparison run where:

- the model server / EPP / gateway plumbing is identical to the WVA
  benchmark,
- WVA does **not** decide replica counts, and
- each variant's HPA scales on a documented, canonical signal so the
  baseline is defensible.

---

## Proposal — adopt the upstream HPA-EPP pattern

The llm-d project publishes a canonical HPA-only autoscaling guide at
[`llm-d/llm-d/guides/workload-autoscaling/README.hpa-epp.md`](https://github.com/llm-d/llm-d/tree/main/guides/workload-autoscaling).
This proposal applies that pattern to a multi-variant deployment and
compares it head-to-head with WVA mode.

A converter script flips a deployed `guides/two-variant-wva` environment
between WVA mode and HPA-only-EPP mode without re-standup:

1. **Disable WVA** —
   `kubectl scale -n $NS deploy/workload-variant-autoscaler-controller-manager --replicas=0`.
   The controller stops emitting `wva_desired_replicas`. Existing VAs and
   the saturation ConfigMap are left in place (harmless when the
   controller is down).

2. **Add two External rules to the WVA-installed prometheus-adapter** so
   HPAs can read the EPP-side signals:

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
         metricsQuery: 'sum(inference_extension_flow_control_queue_size{inference_pool="<pool>"})'

       - seriesQuery: 'inference_objective_running_requests'
         resources:
           overrides:
             namespace: { resource: "namespace" }
         name:
           matches: '^inference_objective_running_requests$'
           as:      'epp_running_requests'
         metricsQuery: 'sum(inference_objective_running_requests{top_level_controller_name="<epp>"})'
   ```

   Applied via `helm upgrade prometheus-adapter --reuse-values --values
   <rule-file>`. Both metrics are model-level sums — they have no
   per-variant attribution. WVA's V2 saturation analyzer already reads the
   same `inference_extension_flow_control_queue_size` and `_queue_bytes`
   metrics (see `internal/collector/registration/saturation.go:108–126`),
   so both modes operate on the same gateway-side signal.

3. **Replace both HPAs** (primary + secondary) with the upstream metric +
   behavior block. The two HPAs differ only in `scaleTargetRef.name`:

   ```yaml
   spec:
     scaleTargetRef:
       kind: Deployment
       name: <existing>             # primary or secondary
     minReplicas: 1
     maxReplicas: 10
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
         policies:
           - type: Percent
             value: 100
             periodSeconds: 15
       scaleDown:
         stabilizationWindowSeconds: 300
         policies:
           - type: Percent
             value: 100
             periodSeconds: 15
   ```

The script saves the original HPA spec to a `hpa-pre-hpaepp-snapshot`
ConfigMap before patching so `--revert` restores the prior state byte-for-byte
(WVA mode, original HPAs, dropped EPP rules).

---

## Threshold tuning — what 250 / 250 means and when to change it

The two `250`s in the upstream HPA spec are **literal threshold values**,
not magic constants. They control how aggressively HPA scales:

- **`epp_queue_size = 250`** with `target.type: Value` — HPA scales to keep
  the *total* model-level gateway queue at ≈250 requests. If the queue
  climbs to 500, HPA wants ~2× the current replicas.
- **`epp_running_requests = 250`** with `target.type: AverageValue` — HPA
  divides the metric value by *its own* current replica count. With 1
  replica it targets 250 in-flight per pod; with 4 replicas, 250 per pod
  too, so the model can carry 1000 concurrent before scale-up.

The upstream chose `250 / 250` for Llama-3.1-8B on a 1–3 replica range.
Our setup uses Llama-3.1-8B too, but with a 1–10 replica range and
different load shapes. **Before running the comparison we need a smoke
test confirming HPA actually engages under the chosen workload rate**,
otherwise "WVA scales / HPA does not scale" is uninformative.

A practical first cut for `prefill_heavy.yaml` at rate=5: lower
`epp_queue_size` to ~`50–100` and keep `epp_running_requests` at `250`
for the smoke test. In our prior WVA run at rate=5 the EPP queue peaked
around ~150, so a `250` threshold would never fire on that signal.

The absolute thresholds do **not** need to match WVA's saturation config
(`kvCacheThreshold: 0.8`, `queueLengthThreshold: 5`) for the comparison to
be fair — those govern WVA's internal logic, not HPA's. What matters is
that both modes operate on the same **gateway-side signal** and both are
actually allowed to scale during the run.

---

## Two HPAs per pool — the structural multi-variant gap

`HPA.spec.scaleTargetRef` points to a **single workload** (one Deployment,
one StatefulSet, one LeaderWorkerSet). It cannot scale two Deployments
coordinatedly. With two variant Deployments we install **two HPAs**:

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

---

## Why a converter script (and not a new scenario yaml)

**Alternative considered (rejected):** add `guides/two-variant-hpa-epp`
plus new jinja templates that render when `wva.enabled: false`.

| | Converter (recommended) | New scenario yaml |
|---|---|---|
| Lines added | ~200 (one script + one rule yaml) | ~350+ in this repo, plus 2 new jinja templates in upstream `llm-d-benchmark` we don't own |
| A/B between modes | one command, no re-standup | requires teardown + re-standup of full stack |
| Drift risk vs. WVA setup | none — plumbing is literally the same | new scenario can drift from the WVA scenario over time |
| Reproducibility on user fork | self-contained on our branch | requires fork + commit on `llm-d-benchmark` too |

The plumbing differs only in the scaling brain — adding a parallel
scenario duplicates everything that isn't different.

---

## Reversibility

Required so a single cluster supports back-to-back A/B runs:

```bash
python hack/benchmark/switch_to_hpa_only.py -n $NS            # WVA -> HPA-EPP
make benchmark-run BENCHMARK_NAMESPACE=$NS BENCHMARK_SPEC=guides/two-variant-wva
python hack/benchmark/switch_to_hpa_only.py -n $NS --revert   # HPA-EPP -> WVA
make benchmark-run BENCHMARK_NAMESPACE=$NS BENCHMARK_SPEC=guides/two-variant-wva
```

`--revert` scales WVA back to 1, removes the two EPP rules via
`helm upgrade --reuse-values --set 'rules.external=null'` (and re-adds the
original `wva_desired_replicas` rule from a snapshot if needed), and
restores both HPAs from the snapshot ConfigMap.

---

## Verification

1. **WVA off** — `kubectl get deploy -n $NS
   workload-variant-autoscaler-controller-manager
   -o jsonpath='{.spec.replicas}'` returns `0`.
2. **External-metrics API exposes both rules** —
   `kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/$NS/epp_queue_size"`
   and `.../epp_running_requests` each return a numeric value.
3. **HPAs read both metrics** — `kubectl describe hpa -n $NS
   unsloth--*-decode` shows
   `Metrics: epp_queue_size on ... <X> / 250` and
   `epp_running_requests on ... <X> / 250 (avg)`, with
   `Conditions: ScalingActive=True`.
4. **Behavior reflects upstream windows** — `scaleUp 0s`, `scaleDown 300s`.
5. **End-to-end** — during a benchmark run, both HPAs scale on the same
   model-level metric values; no `wva_desired_replicas` reads.
6. **Revert** — after `--revert`, HPAs read `wva_desired_replicas` again,
   WVA is at `replicas=1`, and the EPP rules are gone from the adapter
   ConfigMap.

---

## Open questions for team review

1. **Threshold values** — start with upstream `250 / 250`, run a smoke at
   rate=5 to confirm HPA fires, then tune down? Or pick a deliberately
   lower starting point (e.g. `50 / 100`)?
2. **`scaleDown` window** — upstream uses `300 s`; our prior runs used
   `120 s`. Match upstream for fidelity, or keep `120 s` for parity with
   our WVA-mode behavior?
3. **`epp_running_requests` semantics** — `AverageValue` divides by *each
   HPA's* replica count, biasing scale-up toward the variant with fewer
   current replicas (the structural pathology described above). Treat as
   a known limitation of the upstream pattern (it is) and document it,
   or attempt to mitigate with a custom metric query?
4. **Controller image** — the running controller is on the
   `fix-cache-config-info` image. When WVA is scaled to 0 the image is
   inert. Leave it (`--revert` is a single step) or roll back to `v0.6.0`
   for HPA-only mode for cleanliness?
5. **Approach** — converter (recommended) vs. parallel scenario yaml.
   Anyone want the orthogonal scenario yaml for clarity, even at the cost
   of duplicating ~350 lines and adding files to the upstream
   `llm-d-benchmark` repo?

---

## Implementation artifacts (out of scope for this proposal)

To be added in a follow-up implementation turn after this proposal is
approved:

- `hack/benchmark/switch_to_hpa_only.py` — converter, with `--revert`.
- `hack/benchmark/scenarios/guides/hpa-epp-prometheus-adapter-rules.yaml`
  — the two External rules the script applies via `helm upgrade
  --reuse-values --values`.

---

## Appendix — alternative HPA signal: per-pod KV utilization

Earlier drafts of this proposal used `vllm:kv_cache_usage_perc` (target
0.8, `scaleUp.stabilizationWindowSeconds=120`) as the HPA signal instead
of the upstream EPP metrics. We pivoted to the upstream pattern for two
reasons:

- **Authority** — comparing WVA against the exact HPA pattern the llm-d
  community recommends is more defensible than comparing against a
  homegrown KV-threshold scheme.
- **Same gateway-side signal as WVA** — the upstream EPP queue metric is
  exactly one of the inputs WVA's V2 saturation analyzer already reads,
  so the comparison cleanly isolates WVA's cross-variant cost-aware
  optimizer as the differentiator.

The KV-only variant remains technically viable and could be revisited if
a future scenario specifically needs a per-pod signal (e.g. comparing
HPA's reactivity to per-pod load vs. gateway-side queueing). The shape
that variant would take, for the record:

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

Each HPA would read it as External with a per-variant `llm_d_ai_variant`
selector and target `Value: "800m"` (= 0.8). Two-HPAs-per-pool still
applies, so the structural pathology described above is the same; only
the metric source differs.

This routing through the WVA-installed external adapter (rather than the
OpenShift-managed custom-metrics adapter) is necessary because the
cluster has two prometheus-adapter installs:
`openshift-user-workload-monitoring/prometheus-adapter` (serving
`v1beta1.custom.metrics.k8s.io`, not configurable from this repo) and
`workload-variant-autoscaler-monitoring/prometheus-adapter` (serving
`v1beta1.external.metrics.k8s.io`, installed by the WVA standup). The
External path avoids any conflict.

---

## Filename note

This file retains the historical `-kv-` segment in its filename
(`two-variant-hpa-only-kv-experiment.md`) for stability of GitHub URLs
already shared with the team. The experiment itself is now an
"HPA-EPP vs. WVA" comparison, matching upstream pattern naming.
