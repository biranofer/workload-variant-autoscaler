# Discussion: decode Deployments lack graceful termination on scale-down

Draft for internal WVA team discussion before filing upstream. Target repo for the eventual issue: `llm-d/llm-d-model-service`.

## Summary

Decode Deployments rendered by the `llm-d-model-service` chart/controller have no way to configure graceful pod termination — no `terminationGracePeriodSeconds` override, no `preStop` lifecycle hook. Under the Kubernetes default (30s grace period, no preStop), scaling a decode Deployment down mid-request severs any in-flight streaming generation that hasn't finished within 30 seconds, producing hard client-visible errors instead of a graceful drain.

This affects **any** autoscaler that reduces decode replica count — KEDA, a plain HPA, or manual `kubectl scale` — not something specific to one controller's scaling logic.

## Evidence

Found while benchmarking `workload-variant-autoscaler`'s KEDA-based two-variant autoscaling ([#1435](https://github.com/llm-d/llm-d-workload-variant-autoscaler/pull/1435)). Rendered Deployment manifest from a live run (`helm.sh/chart: llm-d-modelservice-v0.4.15`) has zero occurrences of `terminationGracePeriodSeconds` or any `lifecycle:` block — confirmed both in the rendered manifest and via GitHub code search across `llm-d/llm-d-model-service` (0 results for `terminationGracePeriodSeconds`, `preStop`, `lifecycle`, `gracePeriod`, `drain`).

Under a saturation workload (Poisson, 4K/1K tokens, ramping to 10 RPS), end-to-end request latency reached p95=58.7s / p99=66.5s. A run that exercised autoscaling scale-down produced 104 errors (out of 7800 requests) clustering at 31–45 seconds of request latency — just past the Kubernetes default 30s grace period, well under the client's 300s timeout. This is consistent with in-flight decode streams being SIGKILLed when their pod is removed during a scale-down, not a client-side timeout or application crash. See [`comparison-two-variant-20260725/comparison.md`](../../comparison-two-variant-20260725/comparison.md) for the full run.

## Why this needs a chart-level fix, not just a per-scenario override

Kubernetes' termination sequence (SIGTERM → grace period → SIGKILL, endpoint removal from Service/EPP) is a *mechanism*, not a policy — it has to be sized to the workload. The current default (30s, no preStop) is tuned for short-lived HTTP requests, not multi-second-to-tens-of-seconds LLM decode streams. Every deployment of this chart under a scale-capable autoscaler inherits this gap silently: everything looks healthy (no crash, no OOM, controller decision logs are clean) right up until a scale-down event severs live connections.

## Suggested fix

- Expose `terminationGracePeriodSeconds` as a configurable field on the decode (and prefill) pod spec, with a default long enough to cover realistic p99 decode duration for typical workloads (or make it clearly documented as required tuning).
- Add an optional `preStop` lifecycle hook (e.g. `sleep N`) so a pod stops receiving new traffic before it begins shutting down, closing the race between Service/EPP endpoint-list propagation and SIGTERM delivery.
- Document that vLLM/uvicorn's SIGTERM handling should be verified to actually drain in-flight requests rather than dropping connections immediately — the extended grace period only helps if the app-level shutdown behavior cooperates with it.

Open to opening a PR against `llm-d-model-service` if the team agrees this is the right shape of fix — it's a values/template addition, not something we can work around purely from consumer repos.

## Open questions for the team

- Does this belong in `llm-d-model-service` (chart/controller default), or should WVA/KEDA-side benchmarks just document the required per-scenario override as a stopgap?
- Should WVA itself be aware of in-flight requests before removing a pod (e.g. respecting a drain signal), or is that squarely out of scope for an autoscaler and purely a Deployment/chart concern?
