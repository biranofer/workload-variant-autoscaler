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

# Single-variant WVA (Sat-2, scaleDownBoundary=0.60) vs KEDA-EPP — 1000/250 tokens, sustained crossover-rate load

Date: 2026-07-28
Namespace: `biran` (pokprod001)
Model: `unsloth/Meta-Llama-3.1-8B-Instruct`
Harness: inference-perf v0.7.0

Every prior comparison in this series used short (5-minute) stages, long enough to see a single overshoot-and-correct cycle but not long enough to see whether the system *converges* under sustained load. This doc extends the final stage to 20 minutes at rate=24 — a rate just past the crossover point identified in [`comparison-1000x250-r16x40-20260728`](../comparison-1000x250-r16x40-20260728/comparison.md) — specifically to answer: does WVA settle into a stable replica count, or does it keep oscillating indefinitely?

WVA config also changes here: `scaleDownBoundary` lowered from 0.70 to 0.60 (wider hysteresis gap against the unchanged `scaleUpThreshold=0.75`), to test whether more conservative scale-down reduces oscillation.

## Setup

Single TP=1 decode deployment (1 GPU/pod), both legs min=1/max=10. WVA: `eppQueueDemandMultiplier=2.0`, `scaleUpThreshold=0.75`, **`scaleDownBoundary=0.60`** (down from 0.70 in every other doc in this series). KEDA-EPP: `scaledobject-t50.yaml` (queue threshold=50/pod avg, running-requests threshold=16/pod avg).

**Workload** (`prefill_heavy_1000_250_16_20_24x20`, Poisson arrival): input ≈1000 tokens, output ≈250 tokens. 3 stages: 5 min @ rate 16, 5 min @ rate 20, **20 min @ rate 24**. 39600 requests total.

Both runs confirmed clean: zero `FailedScheduling` events during either run's full window, no OOMKilled pods, `oc whoami` verified valid throughout. The WVA run was launched deliberately despite tight cluster-wide GPU headroom at launch time (11 free of 94) — held up fine, no contention materialized. The KEDA-EPP leg's own auxiliary plot-generation step crashed with a broken-pipe error after writing its report (unrelated to the underlying benchmark data or to this doc's own analysis pipeline, which ran cleanly).

## Results

| metric                       | WVA (sdb=0.60) | KEDA-EPP |
|------------------------------|----------------|----------|
| requests                     |          39600 |    39600 |
| errors                       |            209 |       14 |
| error rate                   |          0.53% |    0.04% |
| avg replicas                 |           1.76 |     4.01 |
| max replicas                 |              6 |        5 |
| cost (avg replicas × GPU/hr) |           1.76 |     4.01 |
| avg KV cache utilization     |          16.2% |     3.3% |
| avg EPP queue depth          |            4.7 |      0.0 |
| avg pod startup (s)          |             95 |       94 |
| TTFT p50 (ms)                |             90 |       50 |
| TTFT p95 (ms)                |          4,950 |      110 |
| TTFT p99 (ms)                |          8,310 |      190 |
| Request latency p50 (ms)     |          3,720 |    2,590 |
| Request latency p95 (ms)     |         18,930 |    3,300 |
| Request latency p99 (ms)     |         22,680 |    4,030 |

Per-stage TTFT and error breakdown:

**WVA (sdb=0.60)**

| stage (dur/rate) | p50   | p95   | p99   | errors |
|------------------|-------|-------|-------|--------|
| 0 (5m/16)        | 0.07s | 0.13s | 0.21s |      0 |
| 1 (5m/20)        | 0.10s | 0.21s | 0.30s |      0 |
| 2 (20m/24)       | 0.09s | 5.69s | 8.45s |    209 |

**KEDA-EPP**

| stage (dur/rate) | p50   | p95   | p99   | errors |
|------------------|-------|-------|-------|--------|
| 0 (5m/16)        | 0.05s | 0.11s | 0.22s |      0 |
| 1 (5m/20)        | 0.05s | 0.10s | 0.13s |      0 |
| 2 (20m/24)       | 0.05s | 0.11s | 0.20s |     14 |

Reproducing:

| run      | results dir                                                             |
|----------|-------------------------------------------------------------------------|
| WVA      | `biran-20260728-162745-184/results/inference-perf-1785245307-eho3wx_1/` |
| KEDA-EPP | `biran-20260728-172705-001/results/inference-perf-1785248869-986ve6_1/` |

## Graphs

### WVA — never converges, three full oscillation cycles across the 20-minute sustained stage

![WVA single-variant pipeline](img/wva_pipeline.png)

Stages 0-1 (rates 16, 20) stay flat, matching every prior leg in this series. Once stage 2 (rate=24, 20 minutes) begins, WVA cycles through **three distinct overshoot-and-correct episodes** rather than settling: 1→2→4→1 (peak 4), 1→3→6→1 (peak 6, the largest), 1→2→3→1 (peak 3) — each with KV cache utilization spiking toward 90-100% and dropping back to near-zero. The full controller decision timeline (pulled directly via `EmitReplicaMetrics` logs, before any log-buffer rotation risk) confirms this precisely; it isn't a plotting artifact. The 209 errors cluster across these cycles, not at any single event.

### KEDA-EPP — converges to a stable 5 replicas within the first few minutes, holds for the rest of the stage

![KEDA-EPP single-variant pipeline](img/keda_epp_pipeline.png)

Replica count climbs 1→2→4→5 within the first ~10 minutes (spanning stages 0-1 and the very start of stage 2) and then holds at 5 for nearly the entire 20-minute sustained stage — one small dip to 4 and back, otherwise flat. KV cache utilization settles at 3-8%, and `vLLM requests waiting` stays at exactly 0 for the whole run. This is a genuinely converged steady state, not a lucky snapshot.

## Reading these numbers

**This is the central finding of the whole 1000/250 series, made explicit by extending the stage duration.** Every earlier doc's "overshoot-and-correct" language left open whether WVA would eventually settle if given enough time at a sustained rate. This run answers that: **no, not at this rate, not within 20 minutes.** WVA cycles through three separate overshoot episodes with no sign of damping — the second cycle (peak 6) is larger than the first (peak 4), not smaller, which is the opposite of what a converging system would do. KEDA-EPP, by contrast, converges within about 10 minutes and stays converged for the remaining ~15.

**The lowered `scaleDownBoundary` (0.60 vs the series' usual 0.70) doesn't fix this.** A wider hysteresis gap was the natural first thing to try against oscillation, and it does show up in slightly more gradual scale-down steps (e.g. 6→6→6→5→4→2→1 over multiple ticks rather than a single sharp drop), but it doesn't stop the cycling from recurring — the third cycle starts immediately after the second one's scale-down completes, with no extended stable period in between.

**Cost is still meaningfully lower for WVA (1.76 vs 4.01 avg replicas), but the reliability gap here is the largest yet documented in this series** — 209 vs 14 errors (0.53% vs 0.04%, roughly 13× worse) and a TTFT p99 more than 40× worse (8.31s vs 0.19s). Unlike the shorter-stage rates16-28 and rates16-40 docs, there's no "it resolves once the stage ends" caveat here — the stage was deliberately long enough to rule that out.

## Honest conclusions

1. **WVA does not converge to a stable replica count under sustained load at a rate past the crossover point** — this is the key new finding this longer-duration test was designed to surface. Three full oscillation cycles occurred in 20 minutes with no sign of the amplitude decreasing.
2. **Lowering `scaleDownBoundary` from 0.70 to 0.60 is not sufficient to stop the oscillation** on its own — it changes the shape of individual scale-down transitions (more gradual) but not the underlying cycle-and-repeat pattern. A real fix would likely need either a longer stabilization delay on the scale-up side too, or a smoothing/damping term in the demand estimate itself, neither of which this config change addresses.
3. **KEDA-EPP's one-hop, continuously-reactive design converges where WVA's threshold-gated, two-hop design doesn't**, at a real but bounded cost premium (2.3× more replicas here). This is the most decisive reliability gap documented in the whole series, precisely because it's the first test designed to check for convergence rather than just measure a single transient.
4. **Both runs were clean on the first attempt** despite the WVA leg being launched deliberately under tight GPU headroom (11 free of 94) — a calculated risk that paid off, but not the recommended default going forward.
