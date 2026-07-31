<!--
Table formatting rules for this doc (keep alignment readable in the raw editor):
  1. Every cell has exactly one space of padding inside the pipes (`| cell |`).
  2. Within a table, every row uses the SAME column widths. Widen a column to
     fit its widest cell — don't leave short cells un-padded.
  3. Text columns are left-aligned; numeric columns are right-aligned.
  4. The separator row uses dashes only, with a length matching the column width.
  5. Do not vary column widths between the header, separator, and data rows.
-->

# Rate-anchored k2 (PR #1501) — flag ON vs OFF, sustained 1000/250 prefill-heavy

Date: 2026-07-31
Namespace: `biran` (pokprod001)
Model: `unsloth/Meta-Llama-3.1-8B-Instruct`
Harness: inference-perf v0.7.0
Branch: `validate/rate-anchored-k2`

## Why this doc exists

[PR #1501](https://github.com/llm-d/llm-d-workload-variant-autoscaler/pull/1501) (`feat/rate-anchored-k2`) adds a second, rate-anchored estimator for the V2 saturation analyzer's compute-bound capacity term (`k2`), behind an internal switch (`EnableRateAnchoredK2`). Its own motivation cites this repo's [`comparison-1000x250-16x20x24ext20-20260728`](../comparison-1000x250-16x20x24ext20-20260728/comparison.md) finding: WVA cycling replicas at 16.2% average KV utilization, because the existing `k2` estimator measures a KV **stock** while the real constraint on a prefill-heavy workload is a **rate**. Full problem/approach writeup: [`docs/plans/engine/rate-anchored-k2.md`](../docs/plans/engine/rate-anchored-k2.md) (on the PR branch).

This doc validates the flag against the same sustained 1000/250 workload that motivated the PR, flag ON vs flag OFF, same cluster state, controller restarted between legs to flush in-memory `k2` history.

## Setup

Single TP=1 decode deployment (1 GPU/pod), min=1/max=10, `scaleUpThreshold=0.75`, `scaleDownBoundary=0.60` (live-patched into the saturation ConfigMap), ScaledObject `scaleUp` Percent100/15s (window 0s) / `scaleDown` Percent100/15s (window 300s, shipped default).

Flag toggled via two separately built images (`EnableRateAnchoredK2` is a Go build-time const, not runtime-toggleable): `ghcr.io/biranofer/llm-d-workload-variant-autoscaler:rate-anchored-k2-on` and `...-off`, both built from the same commit (`46c22692`, PR branch merged onto fresh `upstream/main`).

**Workload** (`prefill_heavy_1000_250_16_20_24x20`, Poisson arrival): input 1000±50 tokens, output 250±25 tokens. 3 stages: 5 min @ rate 16, 5 min @ rate 20, 20 min @ rate 24. 39,600 requests total. Both runs confirmed clean (harness reported zero crashes; the error counts below are HTTP-level request failures, not harness failures).

## Results

| metric                    | flag ON | flag OFF |
|----------------------------|--------:|---------:|
| requests                   |  39,600 |   39,600 |
| errors                     |      67 |      132 |
| error rate                 |   0.17% |    0.33% |
| avg replicas                |    2.09 |     1.82 |
| max replicas                |       5 |        4 |
| cost (avg replicas × GPU/hr) |    2.09 |     1.82 |
| avg KV cache utilization     |   12.9% |    15.0% |
| avg EPP queue depth          |    2.19 |     3.54 |
| avg pod startup (s)           |     88 |       86 |
| TTFT mean (s)                |    0.74 |     0.79 |
| TTFT p99 (s)                  |    7.58 |     7.40 |
| Request latency mean (s)       |    6.27 |     6.34 |
| Request latency p99 (s)        |   21.96 |    22.51 |
| ITL mean (s)                    |   0.021 |    0.021 |

Reproducing:

| run       | results dir                                                                                  |
|-----------|-----------------------------------------------------------------------------------------------|
| flag ON   | `biran-20260731-074630-265/results/inference-perf-1785473235-ils2ow_1/`                       |
| flag OFF  | `biran-20260731-083127-585/results/inference-perf-1785475931-dc4q88_1/`                        |

## Replica timeline (decode, collapsed to transitions)

**Flag ON** — 11 transitions over ~33 min:

```
1 → 2 → 1 → 2 → 3 → 4 → 5 → 4 → 1 → 2 → 3 → 1
04:47  :55  05:00 :03  :04  :05  :05  :10  :11  :13  :14  :20
```

One mid-run collapse-to-1 (peak 5 at 05:05:42 → 1 by 05:11:04), recovers to 3, then scales down to 1 at 05:20:06 — lining up with the load profile ending (~30 min after start) rather than a second spurious collapse.

**Flag OFF** — 9 transitions over ~30 min:

```
1 → 2 → 3 → 2 → 1 → 2 → 3 → 4 → 3 → 1
05:32  :45  :46  :51  :52  :55  :55  :57  06:01 :02
```

Same shape: one mid-run collapse-to-1 (peak 3 at 05:46:39 → 1 by 05:52:16), recovers to 4, then scales down to 1 at 06:02:22, again aligned with load-profile end.

## Graphs

Both plots are single-variant runs (no secondary variant deployed) — the flat red "v2" lines in
each panel are inactive/always-zero, not a data gap. The flag-OFF run's `wva_target_timeseries.json`
(WVA-desired overlay) and `capacity_demand_estimate.json` (demand-vs-capacity panel) weren't
generated at the time — `dump_wva_target_timeseries.py` reads live via `kubectl logs`, and by the
time it would have run the controller had already been restarted for the next leg, past the
window. Regenerated both after the fact: the demand-estimate script works from the run's saved
raw vLLM/EPP scrapes (no controller-log dependency), and the WVA-desired series was rebuilt by
replaying the same "Applied saturation decision" pattern against the full controller log already
saved in `results/.../wva-controller.log`.

### Flag ON — ramps to 5, one sharp collapse-to-1, recovers to 3

![WVA flag ON pipeline](img/wva_on_pipeline.png)

Replica count climbs `1→2→3→4→5` as KV utilization and requests-running rise through the rate=20
and rate=24 stages, peaks at 5 while KV cache hits ~100%, then collapses `5→4→1` within about a
minute once the queue drains. Recovers to 3 for the remainder of the sustained stage. The
estimated-demand panel shows two clear bars-exceed-capacity spikes (the two KV-utilization peaks)
where vLLM waiting queue and EPP flow-control queue both spike into the hundreds before draining.

### Flag OFF — ramps to 4, one sharp collapse-to-1, recovers to 4

![WVA flag OFF pipeline](img/wva_off_pipeline.png)

Same shape: `1→2→3` early, drops to 1 as load between stages eases, then climbs `2→3→4` as KV
utilization saturates again (~100%) with vLLM waiting queue peaking near 140 and EPP flow-control
queue peaking near 150 — both higher peaks than the ON run's corresponding spike. Holds at 3-4
through the rest of the sustained stage before the end-of-run drain to 1.

Worth noting: the WVA-desired dashed line visibly **leads** the ready-replica solid line at every
step here (e.g. desired jumps to 3 at ~05:44 before ready follows at ~05:46; desired jumps to 4 at
~05:55 before ready catches up seconds later) — that lag is pod-startup time, not a control-loop
delay, and it's present in both legs.

## Reading these numbers

**Flag ON halves the error rate** (67 vs 132 failed requests) and **lowers EPP queue depth** substantially (2.19 vs 3.54 avg) — consistent with the rate-anchored estimator tracking real arrival throughput rather than an inflated KV-stock history. Request latency p99 is marginally better under ON (21.96s vs 22.51s); TTFT is roughly flat either way (mean slightly better under ON, p99 slightly better under OFF — noise-level).

**The dramatic difference the plan doc predicted did not show up in replica-count stability.** [`docs/plans/engine/rate-anchored-k2.md`](../docs/plans/engine/rate-anchored-k2.md)'s validation section expected the "two overshoot-correct cycles" seen in the original comparison run to *disappear* under flag ON. Instead, both legs here show the **same shape**: one mid-run ramp-to-peak → collapse-to-1 → partial recovery cycle, comparable in timing and amplitude. The oscillation pattern itself doesn't visibly improve at this specific rate profile — the win shows up in error rate and queue depth, not in replica-count behavior.

## Honest conclusions

1. **Flag ON is a real, if modest, improvement on this workload**: half the error rate, lower EPP queue depth, at the cost of slightly higher average replica count (2.09 vs 1.82) — i.e. it trades a little more compute for meaningfully fewer failed requests.
2. **Not yet a decisive result.** This is one run per leg (no repeat), back-to-back on the same cluster with no seed control on the Poisson load — some of the gap could be run-to-run noise. The 2x error-rate gap is likely a real effect; the replica-timeline similarity is a genuine surprise worth a second data point.
3. **Next step**: leg 2 (decode-heavy 100/1000, flag ON) and leg 3 (symmetric 300/300 Poisson, flag ON regression control — must stay flat at 1 replica with zero errors, since a more conservative capacity estimate could turn "correctly holds at one replica" into spurious scale-up). Not started yet; results should be reviewed leg-by-leg, not auto-chained.
