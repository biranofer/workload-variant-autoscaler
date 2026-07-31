# Rate-anchored k2 (PR #1501) — cluster validation results

Validating `EnableRateAnchoredK2` (see [rate-anchored-k2.md](rate-anchored-k2.md)) against the
sustained 1000/250 prefill-heavy workload that motivated the PR, plus decode-heavy and symmetric
regression legs. Branch `validate/rate-anchored-k2`, cluster `pokprod001`, namespace `biran`.

Common config for legs 1a/1b: `scaleUpThreshold=0.75`, `scaleDownBoundary=0.60`, ScaledObject
`scaleUp` Percent100/15s (window 0s) / `scaleDown` Percent100/15s (window 300s, shipped
default), model `unsloth/Meta-Llama-3.1-8B-Instruct`, decode min/max replicas 1/10. Workload
`prefill_heavy_1000_250_16_20_24x20`: Poisson load, rate 16 (300s) → rate 20 (300s) → rate 24
(1200s), input 1000±50 tokens, output 250±25 tokens.

## Leg 1a vs 1b — sustained 1000/250, flag ON vs OFF

| Metric | 1a: flag ON | 1b: flag OFF (baseline) |
|---|---|---|
| Results dir | `biran-20260731-074630-265` | `biran-20260731-083127-585` |
| Run window | 07:46–08:26 | 08:31–11:31 (idle after load completed) |
| Requests: success / fail | 39,533 / **67** | 39,468 / **132** |
| Error rate | 0.17% | 0.33% |
| TTFT mean / p99 (s) | 0.74 / 7.58 | 0.79 / 7.40 |
| Request latency mean / p99 (s) | 6.27 / 21.96 | 6.34 / 22.51 |
| ITL mean / p99 (s) | 0.021 / 0.211 | 0.021 / 0.211 |
| Avg / max replicas | 2.09 / 5 | 1.82 / 4 |
| Avg KV cache utilization | 12.9% | 15.0% |
| Avg EPP queue depth | **2.19** | **3.54** |
| Avg pod startup (s) | 88 | 86 |

### Replica timeline (decode, collapsed to transitions)

**1a (flag ON)** — 11 transitions over ~33 min:
```
1 → 2 → 1 → 2 → 3 → 4 → 5 → 4 → 1 → 2 → 3 → 1
04:47   :55  05:00 :03  :04  :05  :05  :10  :11  :13  :14  :20
```
One mid-run collapse-to-1 (peak 5 at 05:05:42 → 1 by 05:11:04), recovers to 3, then scales down
to 1 at 05:20:06 which lines up with the load profile ending (~30 min after start) rather than a
second spurious collapse.

**1b (flag OFF)** — 9 transitions over ~30 min:
```
1 → 2 → 3 → 2 → 1 → 2 → 3 → 4 → 3 → 1
05:32   :45  :46  :51  :52  :55  :55  :57  06:01 :02
```
Same shape: one mid-run collapse-to-1 (peak 3 at 05:46:39 → 1 by 05:52:16), recovers to 4, then
scales down to 1 at 06:02:22, again aligned with load-profile end.

### Read

Both legs completed cleanly (harness reported zero errors of its own; the failure counts above
are HTTP-level request failures, not harness crashes). Flag ON:

- **Halves the error rate** (67 vs 132 failed requests).
- **Lower EPP queue depth** (2.19 vs 3.54 avg) — the queue backing up less is consistent with
  the rate-anchored estimator tracking real arrival throughput instead of an inflated KV-stock
  history.
- Slightly better p99 request latency (21.96s vs 22.51s), roughly flat TTFT/ITL otherwise.
- **Does not show the dramatic difference the plan doc predicted.** The plan's validation
  section expected the "two overshoot-correct cycles" seen in the original comparison run to
  *disappear* under flag ON. Instead, both legs show the **same shape**: one mid-run
  ramp-to-peak → collapse-to-1 → partial recovery cycle, roughly matched in timing and
  amplitude. The oscillation itself doesn't visibly improve at this specific rate profile —
  the win shows up in error rate and queue depth, not in replica-count stability.

**Caveat:** this is one run per leg (no repeat), and the two legs ran back-to-back on the same
cluster with no seed control on the Poisson load — some of the observed difference could be
run-to-run noise rather than the flag. Given the error-rate gap is 2x, that's likely a real
effect, but the timeline similarity is worth another data point before concluding.

## Remaining legs (not started)

- **Leg 2** — decode-heavy 100/1000, flag ON, scaleDown Pods1/180s window=300s.
- **Leg 3** — symmetric 300/300 Poisson steady state, flag ON, regression control
  (scaleUpThreshold=0.85/scaleDownBoundary=0.70 shipped default). Must stay flat at 1 replica
  with zero errors — any scale-up here would be a regression, since the rate-anchored estimator
  being more conservative could turn "correctly holds at one replica" into spurious scale-up.

User wants to review each leg's results before the next one runs — do not auto-chain legs.
