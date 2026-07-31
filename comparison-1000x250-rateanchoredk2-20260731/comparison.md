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

This doc validates the flag against the same sustained 1000/250 workload that motivated the PR: flag ON vs flag OFF at the shipped-default KEDA `scaleDown` policy, plus a third leg pairing flag ON with the metered `Pods 1/120s` policy that a prior, unrelated sweep ([`comparison-1000x250-16x20x24ext20-20260728`](../comparison-1000x250-16x20x24ext20-20260728/comparison.md)) found to be the single best-performing WVA configuration on this workload. Same cluster state throughout; controller restarted between legs to flush in-memory `k2` history.

## Setup

Single TP=1 decode deployment (1 GPU/pod), min=1/max=10, `scaleUpThreshold=0.75`, `scaleDownBoundary=0.60` (live-patched into the saturation ConfigMap) for all three legs. ScaledObject `scaleUp` Percent100/15s (window 0s) throughout; `scaleDown` is Percent100/15s (window 300s, shipped default) for the ON/OFF legs and `Pods 1/120s` (window 300s) for the third leg.

Flag toggled via two separately built images (`EnableRateAnchoredK2` is a Go build-time const, not runtime-toggleable): `ghcr.io/biranofer/llm-d-workload-variant-autoscaler:rate-anchored-k2-on` and `...-off`, both built from the same commit (`46c22692`, PR branch merged onto fresh `upstream/main`).

**Workload** (`prefill_heavy_1000_250_16_20_24x20`, Poisson arrival): input 1000±50 tokens, output 250±25 tokens. 3 stages: 5 min @ rate 16, 5 min @ rate 20, 20 min @ rate 24. 39,600 requests total. Both runs confirmed clean (harness reported zero crashes; the error counts below are HTTP-level request failures, not harness failures).

## Results

| metric                        | ON, Percent100 | OFF, Percent100 | ON, Pods1/120s |
|--------------------------------|---------------:|----------------:|---------------:|
| requests                       |         39,600 |           39,600 |         39,600 |
| errors                         |             67 |               132 |            123 |
| error rate                     |          0.17% |             0.33% |          0.31% |
| avg replicas                   |           2.09 |              1.82 |           1.67 |
| max replicas                   |              5 |                 4 |              3 |
| cost (avg replicas × GPU/hr)   |           2.09 |              1.82 |           1.67 |
| avg KV cache utilization       |          12.9% |             15.0% |          15.8% |
| avg EPP queue depth            |           2.19 |              3.54 |           2.04 |
| avg pod startup (s)            |             88 |                86 |             95 |
| TTFT mean (s)                  |           0.74 |              0.79 |           0.61 |
| TTFT p50 (s)                   |           0.08 |              0.08 |           0.08 |
| TTFT p90 (s)                   |           3.24 |              3.70 |           1.44 |
| TTFT p99 (s)                   |           7.58 |              7.40 |           8.36 |
| Request latency mean (s)       |           6.27 |              6.34 |           5.87 |
| Request latency p99 (s)        |          21.96 |             22.51 |          23.34 |
| ITL mean (s)                   |          0.021 |              0.021 |          0.020 |

Reproducing:

| run              | results dir                                                              |
|------------------|---------------------------------------------------------------------------|
| ON, Percent100   | `biran-20260731-074630-265/results/inference-perf-1785473235-ils2ow_1/`   |
| OFF, Percent100  | `biran-20260731-083127-585/results/inference-perf-1785475931-dc4q88_1/`   |
| ON, Pods1/120s   | `biran-20260731-163922-172/results/inference-perf-1785505207-0yuu3c_1/`   |

For reference, the historical (pre-PR, effectively flag-off) `Pods1/120s` leg from the prior sweep: 78 errors (0.20%), 1.70 avg replicas, max 3, TTFT p99 4.30s.

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

**Flag ON, Pods1/120s** — 7 transitions over ~33 min, ground-truth `ready_replicas`:

```
1 → 2 → 1 → 2 → 3 → 2 → 1 → 2
13:40  :52  :59  14:02 :02  :08  :10  :13
```

Every descent removes exactly one pod at a time, spaced ~110-120s apart (2→1 at 13:59:04; 3→2 at 14:08:06, then 2→1 at 14:09:57, 111s later) — the metering is real, but since peaks never exceed 3 here, a 2-step metered drain looks almost as fast as a cliff would. The meaningful difference from the Percent100 legs is the **peak size**: max 3 vs 5 (ON) or 4 (OFF), consistent with the historical finding that this policy is the cheapest lever tested on this workload — it just never lets the controller commit to as large a replica count in the first place.

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

Dashed gray vertical lines mark the load-profile's stage transitions (rate 16→20→24, measured
elapsed seconds per stage, not nominal) and the run's end.

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

### Flag ON, Pods1/120s — smallest peak of the three legs, but a bigger error count than expected

![WVA flag ON, Pods1/120s pipeline](img/wva_on_pods1_120s_pipeline.png)

Replica count only reaches 3 (vs 5 and 4 for the Percent100 legs), and the estimated-demand panel
shows the same two saturation spikes as the other legs. vLLM waiting-queue peaks highest of the
three legs here (149 requests vs 92 for ON/Percent100 and 135 for OFF/Percent100) — consistent
with fewer replicas absorbing the same demand — but EPP flow-control queue peaks *lowest* of the
three (143 vs 180 for ON/Percent100 and 146 for OFF/Percent100), so the smaller replica count
doesn't uniformly worsen every queue signal.

## Reading these numbers

**Flag ON halves the error rate** (67 vs 132 failed requests) and **lowers EPP queue depth** substantially (2.19 vs 3.54 avg) — consistent with the rate-anchored estimator tracking real arrival throughput rather than an inflated KV-stock history. Request latency p99 is marginally better under ON (21.96s vs 22.51s); TTFT p50 is identical (0.08s either way), p90 favors ON (3.24s vs 3.70s), p99 slightly favors OFF (7.58s vs 7.40s) — mean/p90 lean ON, p99 is noise-level either way.

**The dramatic difference the plan doc predicted did not show up in replica-count stability.** [`docs/plans/engine/rate-anchored-k2.md`](../docs/plans/engine/rate-anchored-k2.md)'s validation section expected the "two overshoot-correct cycles" seen in the original comparison run to *disappear* under flag ON. Instead, both Percent100 legs here show the **same shape**: one mid-run ramp-to-peak → collapse-to-1 → partial recovery cycle, comparable in timing and amplitude. The oscillation pattern itself doesn't visibly improve at this specific rate profile — the win shows up in error rate and queue depth, not in replica-count behavior.

**Pairing flag ON with the historically best KEDA policy (`Pods1/120s`) does not reproduce that policy's historical result — it's worse on reliability than the pre-PR baseline at the same policy.** The prior sweep found `Pods1/120s` (flag effectively off, since the PR didn't exist yet) to be the single best-performing WVA leg on this workload: 78 errors (0.20%), TTFT p99 4.30s, 1.70 avg replicas. Today's flag-ON leg at the same KEDA policy: 123 errors (0.31%), TTFT p99 8.36s, 1.67 avg replicas — nearly identical cost, but ~58% more errors and ~94% worse TTFT p99. This is the opposite of what pairing the two "good" levers should produce. The likely confound: `GLOBAL_OPT_INTERVAL` was fixed between that sweep and today (30s→15s optimize loop, [[project_pr1487_optimize_interval]]) — a faster reconcile loop could interact differently with a metered scale-down policy than with the historical 30s cadence it was tuned against. This wasn't isolated with a same-day flag-OFF pair at this KEDA policy (not run, per request), so the flag's contribution here can't be separated from the optimize-interval confound or from ordinary run-to-run noise.

## Honest conclusions

1. **Flag ON is a real, if modest, improvement over flag OFF at the shipped-default KEDA policy**: half the error rate, lower EPP queue depth, at the cost of slightly higher average replica count (2.09 vs 1.82) — i.e. it trades a little more compute for meaningfully fewer failed requests.
2. **Flag ON does not carry that improvement over to the historically best-tuned KEDA policy.** At `Pods1/120s`, today's flag-ON leg has more errors and worse TTFT p99 than the historical (pre-PR) leg at the same policy, despite matching its cost almost exactly. Without a same-day flag-OFF pair at this policy, this can't be cleanly attributed to the flag — the `GLOBAL_OPT_INTERVAL` change is a live confound — but it's a concerning enough signal that the "flag ON is a straightforward win" conclusion from leg 1a/1b should not be assumed to generalize to every KEDA configuration.
3. **Not yet a decisive result on any of this.** Every leg here is one run (no repeat), back-to-back on the same cluster with no seed control on the Poisson load — some of every gap could be run-to-run noise. The ON/OFF error-rate gap at Percent100 is a large enough ratio (2x) to likely be real; the Pods1/120s regression against history is a large enough ratio (1.6x) to be worth a repeat before treating it as settled either way.
4. **Next step**: leg 2 (decode-heavy 100/1000, flag ON) and leg 3 (symmetric 300/300 Poisson, flag ON regression control — must stay flat at 1 replica with zero errors, since a more conservative capacity estimate could turn "correctly holds at one replica" into spurious scale-up). Not started yet; results should be reviewed leg-by-leg, not auto-chained.
