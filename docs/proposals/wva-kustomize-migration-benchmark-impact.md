# WVA Helm-to-Kustomize Migration: Impact on the Two-Variant Benchmark

**Status:** Note for team review &nbsp;·&nbsp; **Created:** 2026-05-30

---

## Why this note

`workload-variant-autoscaler` is migrating its install method from a Helm
chart to Kustomize. The benchmark scaffolding we're proposing to upstream
(see [`two-variant-hpa-only-kv-experiment.md`](./two-variant-hpa-only-kv-experiment.md)
and the two-variant PR to `llm-d-benchmark`, issue
[#1425](https://github.com/llm-d/llm-d-benchmark/issues/1425)) was built
against the current Helm install path. This document captures what survives
the migration unchanged, what doesn't, and what we should call out in PRs
and tracking issues.

---

## Three things to know

### 1. The WVA Helm chart is officially deprecated, but not yet gone

```yaml
# charts/workload-variant-autoscaler/Chart.yaml
version: 0.7.0
deprecated: true
```

The repo's [`deploy/README.md`](../../deploy/README.md) tags Helm as
"Legacy" and recommends `kubectl apply -k config/default/` (Kubernetes) or
`kubectl apply -k config/openshift/` (OpenShift). The chart README states
it *"will be removed in the next minor release."* Both methods work today.

### 2. `llm-d-benchmark` standup still uses Helm

```text
llm-d-benchmark/llmdbenchmark/standup/wva.py:114-160
    helm upgrade --install workload-variant-autoscaler ...
```

It also `helm`-installs `prometheus-adapter` via the values rendered from
`config/templates/jinja/21_prometheus-adapter-values.yaml.j2`. The
benchmark framework has not yet adopted the Kustomize-based WVA install.

### 3. There is a real gap that will bite when the chart is removed

The Kustomize tree at `config/` does **not** include the
`vllm-servicemonitor.yaml` we depend on for the `llm_d_ai_variant`
relabeling. Only the chart ships it, at
`charts/workload-variant-autoscaler/templates/vllm-servicemonitor.yaml`.
The Kustomize `config/prometheus/` directory contains only
`servicemonitor.yaml` for the WVA controller's own metrics — not the
vLLM-side one.

**Consequence:** when the Helm chart is removed without porting that
template into Kustomize first, V2 saturation analysis will hit exactly the
`No saturation metrics available — skipping analysis` failure we saw on
day one of this work, on a stock Kustomize install.

---

## Impact on the work in flight

| Artifact | Today | After Helm chart removal |
|---|---|---|
| `feat/two-variant-wva-benchmark` branch on user fork | works end-to-end | Helm bits still resolve, but `vllm-servicemonitor` disappears, breaking the relabeling V2 needs |
| Two-variant PR to `llm-d-benchmark` (Helm-values shape) | works | needs to be reshaped for Kustomize patches / overlays |
| HPA-only-KV converter script (when implemented) | works | the `helm upgrade --reuse-values` step for the adapter rule needs to become a Kustomize patch or `kubectl edit cm` |
| `hack/benchmark/add_variant.py` | unaffected — patches running resources, no Helm or Kustomize involvement | unaffected |
| Documentation on the branch | unaffected | unaffected |

---

## Recommendation

**Don't block the PR on the migration — but call it out, in two places:**

### 1. In the PR description to `llm-d-benchmark`

Add a "Follow-ups" section noting that this PR codifies the *current*
Helm-based WVA install path and that a separate PR will be needed once
`llm-d-benchmark` migrates to the Kustomize-based WVA install. This tells
the benchmark team the scope and avoids them merging something they think
is final.

### 2. In the WVA repo

Open a tracking issue: **"Port `vllm-servicemonitor.yaml` to Kustomize
before Helm chart removal."** This is the blocking item — without that
template in the Kustomize tree, a Kustomize-installed WVA can't see
variant-labeled vLLM metrics, and V2 saturation breaks on a stock install.
The fix itself is a small (~30 line) change but someone has to write and
own it.

---

## If both issues are in flight, the migration story is:

1. **Today** — multi-variant scenarios land in `llm-d-benchmark` against
   the Helm-based WVA install path used by today's standup.
2. **WVA repo follow-up** — port `vllm-servicemonitor.yaml` (and any
   other Helm-only resources) into Kustomize. This unblocks Kustomize-only
   installs of WVA without losing V2 saturation behavior.
3. **`llm-d-benchmark` follow-up** — migrate `wva.py` and the
   prometheus-adapter values jinja from Helm to Kustomize. The
   multi-variant scenarios from step 1 then re-target the new install
   path; the scenario yaml shape may also need light reshaping (Helm
   values → Kustomize overlay patches).

That's a normal, reviewable sequence. None of the three steps blocks the
others longer than is unavoidable.
