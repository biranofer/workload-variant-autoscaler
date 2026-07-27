# Multi-variant WVA-KEDA guide: what exists, what's needed, against which repo

Status summary for the ask: "provide a guide and benchmark scenario for running multi-variant WVA-KEDA benchmarks."

## What already exists and works today — `llm-d/llm-d-workload-variant-autoscaler`, PR #1435

- Scenario file (`hack/benchmark/scenarios/guides/two-variant-wva.yaml`) + script (`hack/benchmark/add_variant.py`) that together provide exactly this: a guide + benchmark scenario for running Sat-v2 (WVA's saturation-v2 cost-aware optimizer) across multiple variants of the same model, with KEDA-driven autoscaling.
- Workflow: standard `benchmark-standup` for the primary variant (via llm-d-benchmark's normal path), then `benchmark-add-variant` layers a second, differently-priced variant into the same InferencePool/EPP.
- As of 2026-07-27, fully fixed and validated. Four independent bugs were found and fixed, all of which were silently invalidating every prior run of this scenario (including PR #1435's own original validation numbers):
  1. A selector bug that blocked scaling entirely (native HPA `AmbiguousSelector`).
  2. A ServiceMonitor TLS mismatch that silently broke WVA's own metrics scrape.
  3. A pod-label mismatch that undercounted WVA's demand estimate by ~2x.
  4. A scenario-yaml key mistake (`inferenceExtension:` instead of the chart's actual `router.epp.*` key) that meant the EPP flow-control feature — and its queue-depth metric — was never actually active in any run.
- With all four fixed, the scenario now demonstrates real cost-aware behavior: the GPU-efficient primary variant scales under load while the cheaper, less-efficient variant stays flat — the actual value proposition Sat-v2 is designed to deliver in a multi-variant setup.
- **Caveat**: this is a bolt-on approach. The second variant is added by `add_variant.py` directly creating/patching Kubernetes objects (Deployment, ScaledObject, labels) after the primary stands up — it is not a first-class feature of the underlying model-serving chart.

## What a *native* guide would need — different, harder, blocked in a different repo

- The actual blocker is `llm-d-incubation/llm-d-modelservice`'s chart schema — it only supports a fixed `decode`/`prefill`/`requester` key structure in its values, not a variant *array*. There is currently no way to express "N variants of the same model" natively in that chart's values.
- Prior art exists upstream: `llm-d-benchmark#1451` attempted something in this direction (stale/closed, not rejected outright — worth revisiting). Related-but-different asks that surfaced during this investigation: `llm-d-modelservice#253` and `workload-variant-autoscaler#1014` (both about multi-*model* serving, not multi-*variant-of-one-model* — a different problem).
- Making this native would require a chart schema change in `llm-d-modelservice` — a different repo, different maintainers, a decision outside this repo's scope.
- This work is paused pending a cross-team discussion that hasn't been initiated yet.

## Bottom line

- If the ask is for something that works *today*: point to PR #1435 in `workload-variant-autoscaler` — it already is the guide + scenario for multi-variant WVA-KEDA benchmarking.
- If the ask is specifically for a *native* multi-variant guide (no bolt-on script, first-class chart support for N variants): that's a separate, larger effort against `llm-d-modelservice`, currently blocked on a chart schema change and an as-yet-unstarted cross-team discussion.
