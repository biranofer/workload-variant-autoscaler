<!--
Table formatting rules for this doc (keep alignment readable in the raw editor):
  1. Every cell has exactly one space of padding inside the pipes (`| cell |`).
  2. Within a table, every row uses the SAME column widths. Widen a column to
     fit its widest cell — don't leave short cells un-padded.
  3. Text columns are left-aligned; numeric columns are right-aligned.
  4. The separator row uses dashes only, with a length matching the column width.
  5. Do not vary column widths between the header, separator, and data rows.
-->

# Rate-anchored k2 (PR #1501) — flag ON vs OFF, symmetric 1000/1000 tokens

Date: 2026-08-01
Namespace: `biran` (pokprod001)
Model: `unsloth/Meta-Llama-3.1-8B-Instruct`
Harness: inference-perf v0.7.0
Branch: `validate/rate-anchored-k2`

## Why this doc exists

Continuation of [`comparison-1000x250-rateanchoredk2-20260731`](../comparison-1000x250-rateanchoredk2-20260731/comparison.md), which validated [PR #1501](https://github.com/llm-d/llm-d-workload-variant-autoscaler/pull/1501)'s rate-anchored `k2` estimator on a **prefill-heavy** workload (1000 input / 250 output tokens). This doc tests a **symmetric** workload (1000 input / 1000 output tokens) instead — same input size, but 4x the output length, and no known-good historical baseline to compare against (this token shape hasn't been run before). Both legs use the PR's updated revision (`89d152e2`, 5 commits ahead of what the prefill-heavy doc's later legs used) and the same `Pods1/120s` KEDA `scaleDown` policy that leg 1c/1c-v2 used there.

## Setup

Single TP=1 decode deployment (1 GPU/pod), min=1/max=10, `scaleUpThreshold=0.75`, `scaleDownBoundary=0.60` (live-patched into the saturation ConfigMap). ScaledObject `scaleUp` Percent100/15s (window 0s), `scaleDown` **Pods 1/120s** (window 300s).

Flag toggled via two separately built images at the **same commit** (`38db3553`, includes the PR's 5 follow-up commits) — only the `EnableRateAnchoredK2` const differs: `ghcr.io/biranofer/llm-d-workload-variant-autoscaler:rate-anchored-k2-on` (`@sha256:866cf900...`) and `...-off` (`@sha256:72189a36...`, rebuilt for this doc — the previously-tagged `-off` image predated the PR update). Both pinned by digest on the controller Deployment to avoid `IfNotPresent` tag-cache staleness.

**Workload** (`symmetric_1000_1000_16_20_24x20`, new scenario file, Poisson arrival): input 1000±100 tokens, output 1000±100 tokens (mirrors the prefill-heavy doc's input distribution onto the output side). Same 3-stage load profile: 5 min @ rate 16, 5 min @ rate 20, 20 min @ rate 24. 39,600 requests total. `per_request` reporting disabled (known OOM risk at this output size, per `prefill_heavy_4k1k_2_4_6_8.yaml.in`'s precedent).

**Caveat up front**: one run per leg, back-to-back on the same cluster, no seed control on the Poisson load. No historical baseline exists for this workload shape to sanity-check against.

## Results

| metric                        |     ON |    OFF |
|--------------------------------|-------:|-------:|
| requests                       | 39,600 | 39,600 |
| errors                         |     55 |    286 |
| error rate                     |  0.14% |  0.72% |
| avg replicas                   |   5.81 |   4.74 |
| max replicas                   |     10 |     10 |
| cost (avg replicas × GPU/hr)   |   5.81 |   4.74 |
| avg KV cache utilization       |   9.6% |  12.8% |
| avg EPP queue depth            |   5.23 |   2.27 |
| avg pod startup (s)            |     96 |     87 |
| TTFT mean (s)                  |   4.79 |   4.91 |
| TTFT p50 (s)                   |   0.07 |   0.08 |
| TTFT p90 (s)                   |   7.40 |   8.02 |
| TTFT p99 (s)                   |  77.25 |  69.30 |
| Request latency mean (s)       |  17.35 |  18.93 |
| Request latency p99 (s)        | 106.30 |  99.66 |
| ITL mean (s)                   |  0.013 |  0.014 |
| Max EPP flow-control queue     |  1,355 |  1,069 |
| Max vLLM waiting requests      |    280 |    296 |

Reproducing:

| run | results dir                                                             |
|-----|--------------------------------------------------------------------------|
| ON  | `biran-20260731-201550-402/results/inference-perf-1785518226-bv6ryv_1/`  |
| OFF | `biran-20260731-222304-530/results/inference-perf-1785525827-gfgk14_1/`  |

## Replica timeline (decode, collapsed to transitions)

**ON** — 14 transitions, ramps to cap fast then a single long drain:

```
1 → 2 → 4 → 8 → 9 → 10 → 9 → 8 → 7 → 6 → 5 → 4 → 3 → 2 → 1
```

Hits the max replica cap (10) within ~5 minutes of load starting — scale-**up** isn't throttled by `Pods1/120s` (that policy only governs scale-down). Drains metered 1-pod-at-a-time from 10 down to 7, then **holds flat at 7 for ~22 minutes** (17:32–17:54) before draining the rest of the way to 1 as load ends. One ramp, one hold, one drain — no second oscillation.

**OFF** — 21 transitions, same fast ramp to cap, but a genuine second cycle:

```
1 → 2 → 3 → 5 → 8 → 10 → 9 → 8 → 7 → 6 → 5 → 4 → 3 → 4 → 5 → 7 → 6 → 5 → 4 → 3 → 2 → 1
```

Also hits 10 within ~5 minutes, drains metered down to 3 by ~19:47 — but then, unlike ON, **re-ramps mid-run** (3→4→5→7 between 19:48–19:51) before draining again to 1 at the end. A real second demand echo appears in the graph (see below) that ON's run either didn't experience or handled without needing to re-scale.

## Graphs

`wva_saturation_utilization`/`wva_kv_cache_tokens` panels are absent from both plots — the WVA
controller's own Prometheus metrics were never scraped for either run (root cause found and
fixed for future runs: the `ServiceMonitor`'s `tlsConfig.serverName` was hardcoded to
`wva-system` instead of the actual `biran` namespace, failing TLS verification on every scrape
attempt — see the feedback note on this bug). Not recoverable for these two runs after the fact.

### ON — one ramp, one long hold, one drain

![WVA flag ON pipeline](img/wva_on_pipeline.png)

The demand spike is concentrated entirely in the first ~6 minutes (KV cache hits 100%, vLLM
waiting queue peaks at 280, EPP flow-control queue peaks at 1,355) — a cold-start effect from a
handful of 1000-output-token requests saturating a single replica before WVA can scale up. Once
replicas catch up, the rest of the ~31-minute load window is calm: queue near 0, ~250 requests
running steadily, holding at 7 replicas for a long stretch before a clean drain.

### OFF — same cold start, plus a second demand echo mid-run

![WVA flag OFF pipeline](img/wva_off_pipeline.png)

Same violent cold-start burst in the first ~6 minutes. But after draining to 3 replicas by
~19:47, a second, smaller demand spike appears around 19:48–19:50 (KV cache climbs back to ~80%,
requests running climbs back to ~650, waiting queue ticks up to ~80) that ON's run never shows —
triggering the second replica ramp described above.

## Reading these numbers

**OFF has 5x the error rate of ON** (286 vs 55, 0.72% vs 0.14%) on this workload — a much larger gap than the prefill-heavy doc's Percent100 comparison (2x). OFF's *average* EPP queue depth is lower than ON's (2.27 vs 5.23), which looks counterintuitive next to OFF's higher error count — but it's explained by OFF holding fewer average replicas overall (4.74 vs 5.81), so its queue reads low across most of a longer, lower-capacity run rather than because demand was genuinely lighter. The *peak* EPP flow-control queue is higher under ON (1,355 vs 1,069), consistent with ON's replica count peaking higher too — average and peak queue depth are telling different stories here, and neither maps cleanly onto the error-count gap.

**The replica-timeline difference is the most interesting result: OFF shows a genuine second oscillation cycle that ON does not.** This is exactly the shape of problem the PR's rate-anchored estimator targets — a demand echo arriving after the KV-stock-based estimator has already decided (incorrectly) that capacity is abundant, versus a rate-based read that would have kept more capacity in reserve. But this can't be treated as proven: with no seed control on the Poisson arrival process, it's entirely possible OFF's run simply drew a harder second burst by chance, unrelated to the flag. The error-rate gap (5x) is large enough to likely be a real effect either way; the replica-timeline story is suggestive but not confirmed by a single run each.

**Tail latency is bad under both flags on this workload, and doesn't favor either one.** TTFT p99 is worse under ON (77.25s vs 69.30s) and request latency p99 is worse under OFF (106.30s vs 99.66s) — both numbers are dominated by the shared cold-start burst in the first few minutes, not by anything the flag changes.

## Honest conclusions

1. **This is a much harder workload than the prefill-heavy series** — both legs hit the 10-replica cap and both show extreme tail latency (TTFT p99 in the 70-80s range) driven by an initial cold-start burst before WVA can scale up. Neither flag setting avoids this; it appears to be inherent to how quickly a single replica saturates on 1000-token outputs.
2. **Flag ON has meaningfully fewer errors on this workload** (55 vs 286, 5x), a bigger gap than seen on the prefill-heavy workload's Percent100 comparison (2x) — directionally consistent with, and stronger than, the prefill-heavy result.
3. **OFF's mid-run second oscillation is a genuine, visually striking difference — but one run each, no seed control, means it isn't proven to be a flag effect rather than workload-random-seed luck.** Worth a repeat if this specific workload shape becomes a priority.
4. **No historical baseline exists for this token shape**, unlike the prefill-heavy series' well-tested 1000/250 profile — so unlike that doc, there's nothing here to sanity-check "is WVA behaving reasonably at all" against, only ON vs OFF against each other.
5. **Metrics gap discovered and fixed for future runs, not backfillable here**: the WVA controller's own Prometheus metrics (`wva_saturation_utilization`, KV-token capacity/used) were never being scraped due to a ServiceMonitor TLS `serverName` bug (hardcoded `wva-system` instead of the real namespace). Fixed live for `biran`; future legs should have this panel available.
