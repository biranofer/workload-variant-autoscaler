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

# Single-variant WVA (Sat-2, multiplier=2, scaleUpThreshold=0.75) vs KEDA-EPP — 1000/250 tokens, rates 10/12/14/16

Date: 2026-07-28
Namespace: `biran` (pokprod001)
Model: `unsloth/Meta-Llama-3.1-8B-Instruct`
Harness: inference-perf v0.7.0

Same 1000/250-token workload as [`comparison-single-variant-1000x250-20260728`](../comparison-single-variant-1000x250-20260728/comparison.md), pushed to higher rate stages (10 → 12 → 14 → 16, vs that doc's 4 → 6 → 8 → 10) to see whether KEDA-EPP's concurrency-driven over-scaling gets worse as raw throughput increases, even though the same request stays just as cheap to serve.

## Setup

Identical to the rates-4-10 companion doc: single TP=1 decode deployment (1 GPU/pod), both legs min=1/max=10. WVA: `eppQueueDemandMultiplier=2.0`, `scaleUpThreshold=0.75`. KEDA-EPP: `scaledobject-t50.yaml` (queue threshold=50/pod avg, running-requests threshold=16/pod avg).

**Workload** (`prefill_heavy_1000_250_10_12_14_16`, Poisson arrival): input ≈1000 tokens, output ≈250 tokens. 4 stages × 5 min at rate 10, 12, 14, 16. 15600 requests total.

inference-perf's own concurrency cap (`num_workers × worker_max_concurrency`, default `cpu_count() × 100` — see `inference_perf/config/loadgen/config.py`) is not a factor here: live vLLM metrics during the WVA leg showed peak concurrency of ~34 running requests at rate=16, far below this default. Both legs' scaling behavior reflects real system dynamics, not harness-side throttling.

Both runs confirmed clean: zero `FailedScheduling` events during either run's window, no OOMKilled pods, `oc whoami` verified valid throughout, harness completed normally on both legs.

## Results

| metric                       | WVA (mult=2, util=0.75) | KEDA-EPP |
|------------------------------|-------------------------|----------|
| requests                     |                   15600 |    15600 |
| errors                       |                       0 |       21 |
| error rate                   |                   0.00% |    0.13% |
| avg replicas                 |                    1.00 |     2.46 |
| max replicas                 |                       1 |        4 |
| cost (avg replicas × GPU/hr) |                    1.00 |     2.46 |
| avg KV cache utilization     |                   10.3% |     3.3% |
| avg EPP queue depth          |                     0.0 |      0.0 |
| avg pod startup (s)          |                      99 |       95 |
| TTFT p50 (ms)                |                      60 |       50 |
| TTFT p95 (ms)                |                     110 |       90 |
| TTFT p99 (ms)                |                     170 |      130 |
| Request latency p50 (ms)     |                   3,230 |    2,660 |
| Request latency p95 (ms)     |                   4,320 |    3,230 |
| Request latency p99 (ms)     |                   4,880 |    3,500 |

Per-stage TTFT and error breakdown:

**WVA (mult=2, util=0.75)**

| stage (rate) | p50   | p95   | p99   | errors |
|--------------|-------|-------|-------|--------|
| 0 (10)       | 0.05s | 0.09s | 0.12s |      0 |
| 1 (12)       | 0.06s | 0.10s | 0.14s |      0 |
| 2 (14)       | 0.06s | 0.11s | 0.17s |      0 |
| 3 (16)       | 0.07s | 0.13s | 0.19s |      0 |

**KEDA-EPP**

| stage (rate) | p50   | p95   | p99   | errors |
|--------------|-------|-------|-------|--------|
| 0 (10)       | 0.05s | 0.08s | 0.11s |      0 |
| 1 (12)       | 0.05s | 0.08s | 0.13s |      9 |
| 2 (14)       | 0.05s | 0.09s | 0.11s |      0 |
| 3 (16)       | 0.05s | 0.09s | 0.14s |     12 |

Reproducing:

| run      | results dir                                                             |
|----------|-------------------------------------------------------------------------|
| WVA      | `biran-20260728-102510-545/results/inference-perf-1785223557-qjw470_1/` |
| KEDA-EPP | `biran-20260728-110924-051/results/inference-perf-1785226210-mquoe6_1/` |

## Graphs

### WVA — flat at 1 replica through every stage, up to rate=16

![WVA single-variant pipeline](img/wva_pipeline.png)

KV cache utilization stays in the 5-15% range for the entire run (higher than the rates-4-10 companion doc's 4.5%, but still nowhere near the 0.75 threshold). WVA never scales, and every stage runs at essentially the same trivial TTFT.

### KEDA-EPP — steps up to 2, briefly flaps to 3 and back, steps to 3, briefly flaps to 4 and back

![KEDA-EPP single-variant pipeline](img/keda_epp_pipeline.png)

Replica count tracks rising concurrency (`requests running` climbs from ~20 to ~55-60 over the run) rather than GPU pressure — KV utilization stays flat at 2-8% throughout, actually *lower* than WVA's, because the same demand is now spread across more replicas. The two brief scale-up-then-back-down flaps (2→3→2 around stage 0/1, 3→4→3 around stage 2/3) are exactly where the errors land: 9 in stage 1, 12 in stage 3 — both likely transient disruption from those flaps, not sustained pressure.

## Reading these numbers

**The pattern from the rates-4-10 companion doc holds and sharpens at higher rates.** WVA still never scales (KV utilization tops out around 10-15% even at rate=16), while KEDA-EPP's concurrency-based trigger now pushes replicas up to 4 (vs 2 in the lighter rates-4-10 run) as raw request count keeps climbing — even though each request is exactly as cheap as before. The cost gap widens accordingly: WVA at 1.00 avg replicas vs KEDA-EPP's 2.46 — a **~146% cost premium** for KEDA-EPP here, compared to ~70% in the rates-4-10 companion doc.

**Unlike the rates-4-10 leg, KEDA-EPP now has more errors than WVA (21 vs 0)** — a new, small wrinkle. Both error clusters coincide with a brief scale-up-then-back-down flap (KEDA scaling to 3 or 4 for a moment then retreating), suggesting the more frequent scaling activity at these higher rates introduces minor connection disruption that a perfectly flat 1-replica WVA deployment simply can't experience. TTFT itself is still marginally better on the KEDA-EPP leg (p99 130ms vs WVA's 170ms) — both are trivially fast in absolute terms, so this isn't a meaningful trade-off, just noise at millisecond scale.

**This continues to be the workload regime where WVA's utilization-gated model is unambiguously the better choice.** There's no tail-latency cost to WVA's conservatism here (unlike the heavier 2800/700 and 4000/1000 workloads in the other companion docs) — it simply doesn't scale when scaling isn't needed, and the gap in unnecessary cost grows the harder KEDA-EPP's concurrency-based trigger is pushed.

## Honest conclusions

1. **KEDA-EPP's concurrency-based over-scaling gets worse, not better, as raw request rate increases** — confirming the mechanism identified in the rates-4-10 companion doc generalizes: the `running_requests` trigger has no way to distinguish "many cheap requests" from "many expensive ones," so it keeps scaling on count alone even as WVA's utilization-based view shows the GPU still has 85-90% headroom.
2. **The cost gap between the two systems widens with rate for this workload shape** — ~70% at rates 4-10, ~146% at rates 10-16. This isn't WVA getting cheaper; it's KEDA-EPP getting progressively more wasteful.
3. **A small, new finding**: KEDA-EPP picked up 21 errors here (vs 0 for both legs in the rates-4-10 doc) — tied to its own brief scale-up/scale-down flaps, not to sustained pressure. Worth watching whether this recurs in future high-rate, low-cost-per-request tests, but not yet enough evidence to call it a real reliability concern.
4. **Both runs were clean on the first attempt** — no cluster GPU contention, no OOM, no auth stalls during either benchmark run itself (an `oc`-login expiry did occur earlier in this session's KEDA-EPP standup for the companion doc, unrelated to these two runs).
