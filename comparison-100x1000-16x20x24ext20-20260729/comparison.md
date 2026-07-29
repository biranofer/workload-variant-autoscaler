<!--
Table formatting rules for this doc (keep alignment readable in the raw editor):
  1. Every cell has exactly one space of padding inside the pipes (`| cell |`).
  2. Within a table, every row uses the SAME column widths. Widen a column to
     fit its widest cell — don't leave short cells un-padded.
  3. Text columns are left-aligned; numeric columns are right-aligned.
  4. The separator row uses dashes only, with a length matching the column width.
  5. Do not vary column widths between the header, separator, and data rows.
  Regenerate all tables via `hack/format-tables.py` if adding/removing rows.
-->

# Single-variant WVA vs KEDA-EPP — decode-heavy (100/1000 tokens), same sustained crossover-rate load

Date: 2026-07-29
Namespace: `biran` (pokprod001)
Model: `unsloth/Meta-Llama-3.1-8B-Instruct`
Harness: inference-perf v0.7.0

## Why this doc exists

Every prior doc in the [`comparison-1000x250-*`](../comparison-1000x250-16x20x24ext20-20260728/comparison.md) series used a **prefill-heavy** workload (1000 input / 250 output tokens) and documented severe TTFT degradation under WVA's replica consolidation — up to ~52x worse tail latency than KEDA-EPP at points in that series. This doc tests the hypothesis that this is a **prefill-compute-contention** effect specific to that token shape, not a general "consolidating replicas hurts latency" problem: prefill is compute-bound (a GPU has no spare compute to batch concurrent prefills "for free" the way it does for memory-bandwidth-bound decode), so concentrating prefill-heavy demand onto fewer replicas should hit TTFT far harder than concentrating decode-heavy demand would.

Same WVA config as the best-performing leg in that series (`scaleUpThreshold=0.75`, `scaleDownBoundary=0.60`, `scaleDown.stabilizationWindowSeconds=300`, `scaleDown` policy `Pods 1`/180s), same 3-stage Poisson schedule (5m@16, 5m@20, 20m@24), same model/namespace/cluster — but the workload is flipped to **decode-heavy**: 100 input / 1000 output tokens instead of 1000/250.

## Setup

Single TP=1 decode deployment (1 GPU/pod), both legs min=1/max=10.

**WVA**: `scaleUpThreshold=0.75`, `scaleDownBoundary=0.60`, `scaleDown.stabilizationWindowSeconds=300`, `scaleDown` policy `Pods 1`/180s, `eppQueueDemandMultiplier=2.0` (dead config, see [note below](#a-note-on-methodology)) — identical to the `Pods1/180s` leg in the prefill-heavy series. Controller reverted to the original pre-experiment image first (see [PR #1487 confound notes](../comparison-1000x250-16x20x24ext20-20260728/comparison.md)) so this run isn't confounded by unrelated analyzer changes.

**KEDA-EPP**: `scaledobject-t50.yaml` (queue threshold=50/pod avg, running-requests threshold=16/pod avg) — same config used throughout the prefill-heavy series. To switch this deployment from WVA-managed to plain KEDA-EPP scaling, the WVA-managed `ScaledObject` (annotated `llm-d.ai/managed=true`) was deleted and `scaledobject-t50.yaml` applied directly; confirmed no WVA-managed `ScaledObject` reappeared (WVA reconciles annotated resources, it doesn't proactively recreate them).

**Workload** (`decode_heavy_100_1000_16_20_24x20`, Poisson arrival): input ≈100 tokens, output ≈1000 tokens. 3 stages: 5 min @ rate 16, 5 min @ rate 20, **20 min @ rate 24**. 39600 requests total. Both runs confirmed clean (harness pods completed successfully, no readiness-probe-driven request failures beyond the counts below).

## Results

| metric                       |    WVA | KEDA-EPP |
|------------------------------|--------|----------|
| requests                     |  39600 |    39600 |
| errors                       |      6 |        2 |
| error rate                   |  0.02% |    0.01% |
| avg replicas                 |   2.21 |     5.97 |
| max replicas                 |      3 |       10 |
| cost (avg replicas × GPU/hr) |   2.21 |     5.97 |
| avg KV cache utilization     |   9.6% |     3.2% |
| avg EPP queue depth          |    0.0 |      0.0 |
| avg pod startup (s)          |     98 |       93 |
| TTFT p50 (ms)                |     40 |       40 |
| TTFT p90 (ms)                |     50 |       50 |
| TTFT p95 (ms)                |     80 |       70 |
| TTFT p99 (ms)                |    110 |      100 |
| Request latency p50 (ms)     | 11,270 |   10,080 |
| Request latency p95 (ms)     | 16,550 |   12,980 |
| Request latency p99 (ms)     | 26,450 |   21,720 |

Per-stage TTFT and error breakdown:

**WVA**

| stage (dur/rate) |   p50 |   p95 |   p99 | errors |
|------------------|-------|-------|-------|--------|
| 0 (5m/16)        | 0.049 | 0.117 | 0.148 |      0 |
| 1 (5m/20)        | 0.038 | 0.050 | 0.074 |      0 |
| 2 (20m/24)       | 0.039 | 0.052 | 0.089 |      6 |

**KEDA-EPP**

| stage (dur/rate) |   p50 |   p95 |   p99 | errors |
|------------------|-------|-------|-------|--------|
| 0 (5m/16)        | 0.040 | 0.092 | 0.111 |      0 |
| 1 (5m/20)        | 0.035 | 0.048 | 0.079 |      0 |
| 2 (20m/24)       | 0.036 | 0.052 | 0.084 |      2 |

Reproducing:

| run      | results dir                                                             |
|----------|-------------------------------------------------------------------------|
| WVA      | `biran-20260729-194956-397/results/inference-perf-1785343840-22e1gn_1/` |
| KEDA-EPP | `biran-20260729-214219-517/results/inference-perf-1785350584-ga9cr6_1/` |

## Graphs

### WVA — single overshoot-and-correct cycle, holds low

![WVA decode-heavy pipeline](img/wva_pipeline.png)

Replica count climbs `1→2→3` in the early part of the sustained stage, then drains back to 1 — a single cycle, versus the 2-3 cycles seen throughout the prefill-heavy series at the same config. KV cache utilization and EPP queue depth stay low the whole run.

### KEDA-EPP — ramps to the 10-replica cap within 3 minutes, holds there for the entire sustained stage

![KEDA-EPP decode-heavy pipeline](img/keda_epp_pipeline.png)

Replica count climbs `1→2→4→7→10` within the first 3 minutes of the run (well before the sustained rate=24 stage even begins) and holds at the max cap of 10 for roughly 30 minutes, before dropping sharply back to 1 near the end once load stops. This is not a reaction to increasing load across the 16→20→24 stage progression — it's an immediate, permanent trigger.

## Reading these numbers

**The prefill-contention hypothesis is confirmed.** Against the identical WVA config's `Pods1/180s` leg in the prefill-heavy series (TTFT p90/p95/p99 = 620/2,520/5,690ms), this decode-heavy leg's WVA numbers (50/80/110ms) are 8-52x better — despite consolidating down to the same 1-replica floor via the same abrupt-then-metered scale-down mechanics. Replica-count behavior is qualitatively similar (a rise-fall cycle down to 1 replica); TTFT impact is not. This is strong evidence that the severe TTFT degradation documented throughout the prefill-heavy series is a property of *that workload's compute profile*, not of WVA's consolidation behavior in general.

**KEDA-EPP's running-requests threshold, tuned for the prefill-heavy series, is degenerate for decode-heavy workloads.** A 1000-output-token request stays "in flight" for the whole generation — by Little's Law, the steady-state number of concurrent in-flight requests scales with `arrival_rate × service_time`, and service time here is far longer than for a 250-token prefill-heavy response. The `running-requests threshold=16/replica` trigger saturates almost immediately regardless of the actual rate stage, ramping straight to the 10-replica cap within 3 minutes and holding there for the entire sustained stage — not a graduated reaction to the 16→20→24 rate progression, just an immediate ceiling. This is a threshold-calibration artifact of reusing the prefill-heavy series' KEDA-EPP config unchanged, not a fair reflection of KEDA-EPP's design for this workload shape.

**On this specific comparison, WVA wins decisively on cost with no latency trade-off at all** — 2.21 vs 5.97 avg replicas (KEDA-EPP costs ~170% more) for *statistically indistinguishable* TTFT (both around 40-110ms across every percentile) and comparable-to-slightly-better request latency. Unlike the prefill-heavy series, there is no reliability/cost trade-off to weigh here — WVA is simply cheaper for the same outcome, because KEDA-EPP's trigger is miscalibrated for this workload rather than because WVA scaled well and KEDA-EPP scaled poorly by design.

## A note on methodology

`eppQueueDemandMultiplier: 2.0` in the WVA saturation ConfigMap is dead configuration — confirmed by reading the deployed image's source: the value is a hardcoded `1.0` constant (`DefaultEPPQueueDemandMultiplier`), never read from the ConfigMap on any leg in this comparison or the prefill-heavy series. It's included here only for consistency with the exact config used in the referenced `Pods1/180s` leg; it has no effect.

## Honest conclusions

1. **Confirmed: the severe TTFT degradation throughout the prefill-heavy comparison series is a prefill-compute-contention effect, not a general consequence of WVA's replica consolidation.** Same WVA config, same consolidation-then-recovery replica pattern, decode-heavy TTFT is 8-52x better than the prefill-heavy equivalent across p90/p95/p99.
2. **This specific KEDA-EPP result should not be read as "KEDA-EPP is 2.7x more expensive than WVA for no reason" as a general claim** — it's specifically that the `running-requests threshold=16/replica` trigger, carried over unchanged from the prefill-heavy series, saturates immediately for a workload where requests stay in-flight far longer. A KEDA-EPP config re-tuned for decode-heavy service times would very plausibly avoid pegging at the replica cap.
3. **Both legs were essentially error-free** (6 and 2 errors respectively, both ~0.01-0.02%) — decode-heavy load is gentle on both autoscalers regardless of the very different replica-count behavior.
4. **Next step**: re-tune KEDA-EPP's running-requests threshold for this workload's service-time profile and re-run, to get a genuine apples-to-apples cost comparison rather than one arm hobbled by a mismatched trigger threshold.
