# Proposal: Adaptive Per-Replica Capacity Fallback in Saturation V2

**Status:** Draft &nbsp;·&nbsp; **Created:** 2026-05-26

---

## Problem

The V2 saturation analyzer needs a per-replica token capacity. The main path reads it from `vllm:cache_config_info`. When that metric is absent (it is in `docker.io/vllm/vllm-openai:v0.9.2`, the image llm-d ships), the fallback in `computeReplicaCapacityFallback` uses `EffectiveMaxBatchedTokens` parsed from deployment args — that's the **per-step compute budget**, not steady-state KV memory.

For Llama-3.1-8B on H100, the two values differ by ~50×:

| | tokens |
|---|---|
| `EffectiveMaxBatchedTokens` (V1 chunked-prefill default) | 8,192 |
| Real KV cache (`6,437 blocks × 64`) | 411,968 |

So the fallback under-states supply by 50×, the analyzer thinks the system is saturated under tiny loads, and the optimizer requests far more replicas than the workload needs. Observed in benchmark: 5 req/s offered (~3 H100s sufficient) drove the optimizer to request 100+ replicas, ramped to `maxReplicas` cap.

## Proposal

When `cache_config_info` is missing, **back-derive total KV capacity from observed runtime state** instead of using the parsed compute budget.

When the pod has load:

```
inferredCapacity = NumRunning × (AvgInputTokens + AvgOutputTokens / 2) / KvCacheUsage
```

This works because at any instant `KvCacheUsage = tokensInCache / TotalKvCapacityTokens`, and we can estimate `tokensInCache` from the in-flight request count and their average token length.

Maintain a small rolling-average store of these observations (per `modelID|accelerator|outputBucket`, same key shape `computeK2` already uses). On reconcile, take the highest-priority signal that has data:

```
1. Live calibration       — current snapshot, if KvUsage ≥ 0.20 and NumRunning > 0
2. Historical mean        — rolling average of prior live samples
3. Iteration-tokens peak  — max non-empty bucket of vllm:iteration_tokens_total
                            (more accurate than parsed EffectiveMaxBatchedTokens)
4. Existing fallback      — current behavior, last resort
```

Then `EffectiveCapacity = capacity × kvCacheThreshold` (unchanged).

### Why this works

Plugged in with the benchmark numbers (NumRunning=80/pod, AvgIn=4000, AvgOut=1000, KvUsage=1.0):

```
inferredCapacity   = 80 × 4500 / 1.0          = 360,000 tokens/pod
After threshold:                              ≈ 288,000
requiredCapacity / 288,000                    ≈ 1 extra pod  → ≈ 3 total
```

That matches the empirical steady-state need (~1.5 req/s/pod sustained on H100).

## Implementation

Three small changes:

1. **`internal/collector/replica_metrics.go`** — plumb `IterationTokensHistogram` through `ReplicaMetrics` (already collects `KvCacheUsage`, `NumRunning`, `AvgInputTokens`, `AvgOutputTokens`).
2. **`internal/engines/analyzers/saturation_v2/analyzer.go`** — replace the body of `computeReplicaCapacityFallback` with the priority chain above. Add a `learnedMemCapacityHistory` map on the analyzer struct (parallel to existing `computeCapacityHistory`).
3. **`SaturationScalingConfig`** — add `enableLiveCalibration: true` (default) so behavior can be opted out.

Mirrors the existing 4-priority pattern in `computeK2`. No new vLLM features required.

## Testing

- Unit: synthetic `ReplicaMetrics` covering each priority tier (live / historical / histogram / derived / no-data).
- E2E: run a prefill-heavy workload against an image without `cache_config_info`; assert replicas converge to steady-state (≈3 for 5 req/s on H100) instead of saturating at `maxReplicas`.

## Risks

- **Cold-start noise**: require `KvUsage ≥ 0.20` and ≥3 history samples before trusting calibration; otherwise fall through.
- **Behavior change**: gated by `enableLiveCalibration` (default on for new installs, opt-out via configmap).
- **Unbounded history**: cap map size, expire entries older than configurable window.
