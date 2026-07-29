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

# Single-variant WVA (Sat-2) vs KEDA-EPP — 1000/250 tokens, sustained crossover-rate load

Date: 2026-07-28 (WVA su=0.85/sdb=0.50 leg added 2026-07-29)
Namespace: `biran` (pokprod001)
Model: `unsloth/Meta-Llama-3.1-8B-Instruct`
Harness: inference-perf v0.7.0

Every prior comparison in this series used short (5-minute) stages, long enough to see a single overshoot-and-correct cycle but not long enough to see whether the system *converges* under sustained load. This doc extends the final stage to 20 minutes at rate=24 — a rate just past the crossover point identified in [`comparison-1000x250-r16x40-20260728`](../comparison-1000x250-r16x40-20260728/comparison.md) — specifically to answer: does WVA settle into a stable replica count, or does it keep oscillating indefinitely?

Three WVA legs are included. The first two are both at `scaleDownBoundary=0.60` (down from 0.70 elsewhere in this series): one at KEDA's default `scaleDown.stabilizationWindowSeconds=120`, one with that window extended to `300` — testing whether a longer scale-down delay damps the oscillation rather than just changing its shape. The third leg keeps the window at `300` but widens the hysteresis gap itself: `scaleUpThreshold=0.85` (up from 0.75), `scaleDownBoundary=0.50` (down from 0.60) — testing whether a wider dead zone between the scale-up and scale-down triggers, rather than a longer post-peak hold, is what actually damps the cycling.

## Setup

Single TP=1 decode deployment (1 GPU/pod), all four legs min=1/max=10. WVA (all three legs): `eppQueueDemandMultiplier=2.0`. KEDA-EPP: `scaledobject-t50.yaml` (queue threshold=50/pod avg, running-requests threshold=16/pod avg).

The three WVA legs differ only in the live-patched `wva-saturation-scaling-config` ConfigMap and the KEDA `ScaledObject`'s `advanced.horizontalPodAutoscalerConfig.behavior.scaleDown.stabilizationWindowSeconds`:

| WVA leg             | scaleUpThreshold | scaleDownBoundary | scaleDown window |
|---------------------|------------------|-------------------|------------------|
| sdw=120s (baseline) |             0.75 |              0.60 | 120s             |
| sdw=300s            |             0.75 |              0.60 | 300s             |
| su=0.85/sdb=0.50    |             0.85 |              0.50 | 300s             |

None of these are exposed by `add_variant.py` as flags; the ConfigMap was patched via `kubectl patch configmap wva-saturation-scaling-config`, the `ScaledObject`'s window via a direct live patch, both confirmed to propagate to the controller (config re-read on next reconcile) and to the underlying native `HorizontalPodAutoscaler` object (`oc get hpa -o jsonpath='{.spec.behavior.scaleDown.stabilizationWindowSeconds}'`) respectively. The WVA controller was restarted before each leg to flush in-memory k2 saturation history.

**Workload** (`prefill_heavy_1000_250_16_20_24x20`, Poisson arrival): input ≈1000 tokens, output ≈250 tokens. 3 stages: 5 min @ rate 16, 5 min @ rate 20, **20 min @ rate 24**. 39600 requests total.

All four runs confirmed clean: zero `FailedScheduling` events during any run's full window, no OOMKilled pods, `oc whoami` verified valid throughout. The first WVA leg (120s window) was launched deliberately despite tight cluster-wide GPU headroom at launch time (11 free of 94) — held up fine, no contention materialized. The KEDA-EPP leg's own auxiliary plot-generation step crashed with a broken-pipe error after writing its report (unrelated to the underlying benchmark data or to this doc's own analysis pipeline, which ran cleanly on all four legs).

## Results

| metric                       | WVA (sdw=120s) | WVA (sdw=300s) | WVA (su=0.85/sdb=0.50) | KEDA-EPP |
|------------------------------|----------------|----------------|------------------------|----------|
| requests                     |          39600 |          39600 |                  39600 |    39600 |
| errors                       |            209 |            111 |                    130 |       14 |
| error rate                   |          0.53% |          0.28% |                  0.33% |    0.04% |
| avg replicas                 |           1.76 |           2.27 |                   2.91 |     4.01 |
| max replicas                 |              6 |              6 |                      7 |        5 |
| cost (avg replicas × GPU/hr) |           1.76 |           2.27 |                   2.91 |     4.01 |
| avg KV cache utilization     |          16.2% |          12.1% |                   9.2% |     3.3% |
| avg EPP queue depth          |            4.7 |            2.0 |                    4.7 |      0.0 |
| avg pod startup (s)          |             95 |             89 |                     93 |       94 |
| TTFT p50 (ms)                |             90 |             80 |                     80 |       50 |
| TTFT p95 (ms)                |          4,950 |          4,110 |                  6,180 |      110 |
| TTFT p99 (ms)                |          8,310 |          6,760 |                  8,240 |      190 |
| Request latency p50 (ms)     |          3,720 |          3,300 |                  3,430 |    2,590 |
| Request latency p95 (ms)     |         18,930 |         17,780 |                 20,000 |    3,300 |
| Request latency p99 (ms)     |         22,680 |         21,290 |                 23,610 |    4,030 |

Per-stage TTFT and error breakdown:

**WVA (sdw=120s)**

| stage (dur/rate) | p50   | p95   | p99   | errors |
|------------------|-------|-------|-------|--------|
| 0 (5m/16)        | 0.07s | 0.13s | 0.21s |      0 |
| 1 (5m/20)        | 0.10s | 0.21s | 0.30s |      0 |
| 2 (20m/24)       | 0.09s | 5.69s | 8.45s |    209 |

**WVA (sdw=300s)**

| stage (dur/rate) | p50   | p95   | p99   | errors |
|------------------|-------|-------|-------|--------|
| 0 (5m/16)        | 0.07s | 0.13s | 0.20s |      0 |
| 1 (5m/20)        | 0.10s | 0.19s | 0.25s |      0 |
| 2 (20m/24)       | 0.07s | 4.83s | 6.95s |    111 |

**WVA (su=0.85/sdb=0.50)**

| stage (dur/rate) | p50   | p95   | p99   | errors |
|------------------|-------|-------|-------|--------|
| 0 (5m/16)        | 0.07s | 0.13s | 0.22s |      0 |
| 1 (5m/20)        | 0.09s | 0.19s | 0.27s |      0 |
| 2 (20m/24)       | 0.07s | 6.83s | 8.58s |    130 |

**KEDA-EPP**

| stage (dur/rate) | p50   | p95   | p99   | errors |
|------------------|-------|-------|-------|--------|
| 0 (5m/16)        | 0.05s | 0.11s | 0.22s |      0 |
| 1 (5m/20)        | 0.05s | 0.10s | 0.13s |      0 |
| 2 (20m/24)       | 0.05s | 0.11s | 0.20s |     14 |

Reproducing:

| run                    | results dir                                                             |
|------------------------|-------------------------------------------------------------------------|
| WVA (sdw=120s)         | `biran-20260728-162745-184/results/inference-perf-1785245307-eho3wx_1/` |
| WVA (sdw=300s)         | `biran-20260728-184029-341/results/inference-perf-1785253270-supi0p_1/` |
| WVA (su=0.85/sdb=0.50) | `biran-20260729-103739-356/results/inference-perf-1785310714-78cl5k_1/` |
| KEDA-EPP               | `biran-20260728-172705-001/results/inference-perf-1785248869-986ve6_1/` |

## Graphs

### WVA (sdw=120s) — never converges, three full oscillation cycles across the 20-minute sustained stage

![WVA sdw=120s single-variant pipeline](img/wva_pipeline.png)

Stages 0-1 (rates 16, 20) stay flat, matching every prior leg in this series. Once stage 2 (rate=24, 20 minutes) begins, WVA cycles through **three distinct overshoot-and-correct episodes** rather than settling: 1→2→4→1 (peak 4), 1→3→6→1 (peak 6, the largest), 1→2→3→1 (peak 3) — each with KV cache utilization spiking toward 90-100% and dropping back to near-zero. The full controller decision timeline (pulled directly via `EmitReplicaMetrics` logs, before any log-buffer rotation risk) confirms this precisely; it isn't a plotting artifact. The 209 errors cluster across these cycles, not at any single event.

### WVA (sdw=300s) — same three cycles, but each peak is held much longer before scaling down

![WVA sdw=300s single-variant pipeline](img/wva_sdw300_pipeline.png)

Still **three** oscillation cycles (peaks 3, 6, 2 — nearly identical magnitudes to the 120s run), so extending the scale-down window does not reduce how often WVA re-triggers a scale-up. What changes is the *shape* of each cycle: once replicas reach a peak, they're held there for ~5+ minutes before starting to decrease (vs ~2-3 minutes with the 120s window) — the stabilization window doing exactly what it's designed to do. The wider, flatter plateaus at the top of each cycle in the replica-count panel are the direct visual signature of this.

### WVA (su=0.85/sdb=0.50) — same three cycles, but wider hysteresis produces *bigger* peaks, not smaller

![WVA su=0.85/sdb=0.50 single-variant pipeline](img/wva_su85sdb50_pipeline.png)

Still **three** oscillation cycles, at t+11-14min, t+19.5-23min, and t+28.5-31.5min into the run — same cadence as both other WVA legs. But the peaks are the largest of any leg in this series: 1→2→4→**5**→1, then 1→3→**7**→1 (7 is the highest replica count seen anywhere in this doc), then 1→2→3→**4**→2→1. Widening the gap between `scaleUpThreshold` (0.75→0.85) and `scaleDownBoundary` (0.60→0.50) made the controller wait for a more extreme utilization reading before each scale-up — and by the time it reacts, demand has piled up further, so the corrective jump overshoots by more, not less. Avg replicas rose again, to 2.91 (the highest of the three WVA legs), and errors (130) landed between the sdw=120s and sdw=300s legs rather than below both.

### KEDA-EPP — converges to a stable 5 replicas within the first few minutes, holds for the rest of the stage

![KEDA-EPP single-variant pipeline](img/keda_epp_pipeline.png)

Replica count climbs 1→2→4→5 within the first ~10 minutes (spanning stages 0-1 and the very start of stage 2) and then holds at 5 for nearly the entire 20-minute sustained stage — one small dip to 4 and back, otherwise flat. KV cache utilization settles at 3-8%, and `vLLM requests waiting` stays at exactly 0 for the whole run. This is a genuinely converged steady state, not a lucky snapshot.

## Reading these numbers

**This is the central finding of the whole 1000/250 series, made explicit by extending the stage duration.** Every earlier doc's "overshoot-and-correct" language left open whether WVA would eventually settle if given enough time at a sustained rate. This run answers that: **no, not at this rate, not within 20 minutes, and not by widening the scale-down window or the threshold gap either.** All three WVA legs cycle through three separate overshoot episodes with no sign of damping in the cycle count. KEDA-EPP, by contrast, converges within about 10 minutes and stays converged for the remaining ~15.

**Extending `scaleDown.stabilizationWindowSeconds` from 120 to 300 trades cost for reliability, without fixing the underlying cycling.** Avg replicas rose 1.76→2.27 (+29%) but errors fell 209→111 (-47%) and TTFT p99 improved 8.31s→6.76s. The mechanism is straightforward: holding replicas up longer after a peak means fewer of the disruptive full scale-down-to-1 events that (per the earlier companion doc's root-cause analysis) sever in-flight streams and produce `ClientPayloadError`-style failures. It's a real, usable lever for trading cost against reliability — but it does not stop new cycles from starting, since the scale-*up* side (which actually re-triggers each cycle) is unaffected by this parameter.

**Widening the threshold gap itself (`scaleUpThreshold` 0.75→0.85, `scaleDownBoundary` 0.60→0.50, on top of the 300s window) makes things *worse* on both axes, not better.** This was meant to test a different hypothesis than the window change — that a wider dead zone between scale-up and scale-down triggers, rather than a longer post-peak hold, would damp the cycling. It doesn't: avg replicas rose further to 2.91 (+28% over the sdw=300s leg, +65% over baseline) and errors rose to 130 (+17% over the sdw=300s leg, though still well below the sdw=120s baseline's 209), with the single largest replica peak of the whole series (7, vs 6 in both other WVA legs). The mechanism is the opposite of what was hoped for: delaying the scale-up decision until utilization reads even higher just means demand has piled up further by the time the controller reacts, so the corrective jump is bigger, not smaller.

**Cost is still meaningfully lower for all three WVA configurations than KEDA-EPP (1.76, 2.27, and 2.91 vs 4.01 avg replicas), but the reliability gap remains the largest documented in this series even at the best-performing 300s setting** — 111 vs 14 errors (0.28% vs 0.04%, ~7× worse) and a TTFT p99 still ~35× worse (6.76s vs 0.19s). Unlike the shorter-stage rates16-28 and rates16-40 docs, there's no "it resolves once the stage ends" caveat here — the stage was deliberately long enough to rule that out for any of the three WVA configurations.

## Honest conclusions

1. **WVA does not converge to a stable replica count under sustained load at a rate past the crossover point, regardless of the scale-down window or threshold-gap setting tested** — this is the key new finding this longer-duration test was designed to surface. Three full oscillation cycles occurred in 20 minutes in all three WVA legs, with no sign of the cycle count decreasing.
2. **Widening `scaleDown.stabilizationWindowSeconds` (120→300) is a genuine cost/reliability lever, not a fix.** It measurably reduces errors (-47%) and improves TTFT p99, at a real cost premium (+29% avg replicas) — worth using if trading some cost for fewer disruptive scale-downs is acceptable, but it does not address the root cause (repeated scale-up re-triggering).
3. **Widening the threshold gap (`scaleUpThreshold` 0.75→0.85, `scaleDownBoundary` 0.60→0.50) is not a usable lever at all — it costs more (2.91 vs 2.27 avg replicas) *and* is less reliable (130 vs 111 errors) than the 300s-window leg it was layered on top of.** A later, more extreme scale-up trigger lets demand overshoot further before the controller reacts, producing bigger corrective jumps (peak 7, the largest in the series) rather than a calmer system. Lowering `scaleDownBoundary` from 0.70 to 0.60 (tested in the prior version of this doc) was already shown to be insufficient on its own; pushing it further to 0.50 in combination with a higher scale-up threshold makes the picture worse, not better.
4. **KEDA-EPP's one-hop, continuously-reactive design converges where WVA's threshold-gated, two-hop design doesn't**, at a real but bounded cost premium (1.4-2.3× more replicas than KEDA-EPP's already-converged 4.01, depending on which WVA configuration is compared). This is the most decisive reliability gap documented in the whole series, precisely because it's the first test designed to check for convergence rather than just measure a single transient.
5. **All four runs were clean on the first attempt** despite the first WVA leg being launched deliberately under tight GPU headroom (11 free of 94) — a calculated risk that paid off, but not the recommended default going forward.
