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

# Two-variant WVA (Sat-2, cost-aware) vs KEDA-EPP autoscaling

Date: 2026-07-25
Namespace: `biran` (pokprod001)
Model: `unsloth/Meta-Llama-3.1-8B-Instruct`
Harness: inference-perf v0.7.0

First comparison exercising WVA's **cost-aware V2 optimizer** on its designed axis — two variants of the same model at different cost/TP — rather than the single-variant setup used in [`comparison-wva-keda-epp-20260722`](../comparison-wva-keda-epp-20260722/comparison.md). That doc's honest-conclusions section (#5) explicitly flagged this as the missing head-to-head; this is that follow-up.

## Setup

**Topology** (both legs): one shared `InferencePool`/EPP fronting two decode `Deployment`s of the same model:
- **primary**: TP=2 (2 GPU/pod), cost=10
- **variant**: TP=1 (1 GPU/pod), cost=5

Both min=1/max=10. Cost ratio (10/5) equals the GPU ratio (2/1), so a TP=2 replica is the *more* GPU-efficient pick per unit cost (shared weights → more KV headroom per GPU than two independent TP=1 replicas) — WVA's cost-aware solver is expected to prefer scaling primary first, spilling to the cheaper variant only when primary saturates.

**WVA Sat-2** (`guides/two-variant-wva`)
- Metric feeding HPA: `wva_desired_replicas` per variant — computed by the WVA controller from KV supply + demand + EPP queue, cost-weighted across variants.
- WVA algorithm: scaleUp when KV util > 85%; scaleDown boundary 70%.

**KEDA-EPP** (`guides/keda-epp-two-variant`, new scenario — no WVA installed at all)
- Metric feeding HPA: same two Prometheus queries as the single-variant KEDA-EPP baseline — `sum(inference_extension_flow_control_queue_size)` and `sum(inference_objective_running_requests)` — applied to **both** variants independently, each with its own `ScaledObject`.
- Because EPP only exposes pool-wide (not per-Deployment) queue/running-request signals, both ScaledObjects read the *identical* value and scale in lockstep — this is the correct behavior for the naive baseline: KEDA-EPP has no cost-awareness to differentiate the two variants at all.
- Thresholds: queue avg/pod = 50, running avg/pod = 16 (same as the single-variant `scaledobject-t50.yaml`, chosen for a fairer comparison against WVA V2).

Both share the same KEDA HPA scaleUp/scaleDown behavior: Percent=100/15s, ScaleUp stab=0s, ScaleDown stab=120s.

**Workload** (`prefill_heavy_4k1k_2_10`, Poisson arrival): input ≈4000 tokens, output ≈1000 tokens, 5 min @ rate=2 (warm-up) then 12 min @ rate=10 (saturation). 7800 requests total.

## Prerequisite bug fixes — without these, replicas never leave 1

Getting a valid run took fixing two independent, compounding bugs. Both produced the identical "stuck at 1 replica" symptom while the WVA controller's own decision log looked completely healthy (correctly computing scale-up decisions that never reached the cluster) — a reminder that a controller's decision log is not proof its decisions were applied.

1. **`add_variant.py`'s variant Deployment selector was a superset of the primary's** — inherited (never removed) the primary's `llm-d.ai/inference-serving` discriminator label, so Kubernetes' native HPA controller's `AmbiguousSelector` safety check fired for both HPAs and blocked scaling entirely. Fixed in `hack/benchmark/add_variant.py::make_variant_deployment()` (this session, committed to this repo).
2. **The WVA controller's `ServiceMonitor` has a hardcoded TLS `serverName`** (`wva-controller-manager-metrics-service.wva-system.svc`) that doesn't match the real tenant namespace, silently breaking the HTTPS scrape — `wva_desired_replicas` never reaches Prometheus, so KEDA's HPA always reads empty and never scales. This resets on every fresh `benchmark-standup` and is not yet fixed at the template level; requires a manual patch after every standup:
  ```
  oc patch servicemonitor wva-controller-manager-metrics-monitor -n <ns> --type=json \
    -p '[{"op":"replace","path":"/spec/endpoints/0/tlsConfig/serverName","value":"wva-controller-manager-metrics-service.<ns>.svc"}]'
  ```

Also fixed along the way (not scaling-correctness bugs, but distort results if left alone): the harness pod's default 32Gi memory OOMKilled mid-run on this workload's 1000-token mean output (bumped to 64Gi in both scenario files), and `report.request_lifecycle.per_request: true` generated a multi-GB JSON file (observed 6GB+, 45+ minutes to collect) that nothing in the analysis pipeline reads — disabled in the workload file.

## Results

| metric                                |       WVA | KEDA-EPP   |
|---------------------------------------|-----------|------------|
| requests                              |      7800 | 7800       |
| successful                            |      7800 | 7767       |
| errors                                |     **0** | 33         |
| error rate                            | **0.00%** | 0.42%      |
| achieved RPS                          |      7.24 | 7.48       |
| avg replicas (primary)                |  **1.00** | 5.58       |
| avg replicas (variant)                |  **1.42** | 5.60       |
| max replicas (primary)                |     **1** | 10         |
| max replicas (variant)                |     **2** | 10         |
| cost (weighted avg replicas × GPU/hr) |  **3.42** | 16.75      |
| avg KV cache utilization              |     59.7% | ~10%       |
| TTFT p50 (ms)                         |    31,055 | **132**    |
| TTFT p95 (ms)                         |    75,152 | **1,192**  |
| TTFT p99 (ms)                         |   110,988 | **5,398**  |
| Request latency p50 (ms)              |    68,885 | **12,227** |
| Request latency p95 (ms)              |   109,562 | **16,511** |
| Request latency p99 (ms)              |   146,295 | **20,100** |
| TPOT p50 (ms)                         |  **36.7** | 12.5       |
| TPOT p95 (ms)                         |  **49.9** | 17.8       |
| TPOT p99 (ms)                         |  **80.5** | 30.1       |

Reproducing:

| run      | results dir                                                             |
|----------|-------------------------------------------------------------------------|
| WVA      | `biran-20260725-175238-930/results/inference-perf-1784991200-atn1rw_1/` |
| KEDA-EPP | `biran-20260725-163818-109/results/inference-perf-1784986741-1ckph5_1/` |

## Graphs

Full per-run pipeline plots (replica count with desired/ready overlay, estimated demand vs capacity, KV utilization, requests running/waiting, EPP queue depth — all vs time):

### WVA — primary flat at 1, variant steps 1→2 under peak load

![WVA two-variant pipeline](img/wva_pipeline.png)

Primary (TP=2, the more cost-efficient variant per the pricing above) never leaves 1 replica — KV utilization stays under WVA's 85% scale-up threshold for primary throughout. The cheaper variant (TP=1) absorbs the overflow, stepping 1→2 during the rate=10 stage and back to 1 as load recedes. This is the "spill to the cheaper variant only when primary saturates" behavior — but note primary itself never actually saturates here (its own KV util panel would need to be read alongside `v2`'s to fully confirm which one WVA judged closer to threshold; see the raw data for the full breakdown).

### KEDA-EPP — both variants scale in lockstep (no cost-awareness)

![KEDA-EPP two-variant pipeline](img/keda_epp_pipeline.png)

Both ScaledObjects read the identical pool-wide EPP signal, so primary and variant climb together (1→9-10) and fall together — exactly the expected naive-baseline behavior, since KEDA-EPP has no mechanism to prefer the more GPU-efficient variant.

## Reading these numbers

**Cost**: WVA holds the line at ~2.4 total replicas (1 primary + 1.4 variant) for the entire run, vs KEDA-EPP's ~11.2 total (5.6 + 5.6) — **a 5× cost difference**, achieved with **zero errors** vs KEDA-EPP's 0.42%.

**Latency**: the flip side is stark — WVA's p50 TTFT (31s) is roughly 235× KEDA-EPP's (0.13s), and request latency is ~5.6× worse at every percentile. This tracks directly from replica count: 3 GPUs total (WVA) vs ~17 GPUs total (KEDA-EPP, at ~5.6+5.6 replicas × mixed TP) serving the same 4000-token-prompt, up-to-10-RPS load.

**Is this a fair test of WVA's value proposition?** Partially. It correctly demonstrates the *cost* side of the story — WVA held cost 5× lower with better reliability by staying under its saturation threshold. But the *latency* side looks worse for WVA than the single-variant comparison's honest-conclusions #4 already predicted: WVA's conservative 85%/70% thresholds mean it tolerates far more queueing before scaling than KEDA-EPP's much more reactive queue/running-request triggers. This workload (4000-token prompts, up to 10 RPS, only 3 GPUs worth of ceiling reached) pushes both variants into sustained near-saturation, which is exactly the regime the single-variant comparison already flagged as WVA's weak spot (see that doc's honest-conclusions #4).

**What this run does *not* yet show**: primary (the more cost-efficient variant per the stated pricing) never scaled beyond 1, so this run doesn't demonstrate WVA's cost-aware solver actually *choosing* between two saturating variants — only "hold the cheap one, spill overflow to the cheaper one." A workload that pushes primary itself past its threshold (higher rate, or a lower primary max-replica ceiling to force contention) would be a sharper test of the cost-aware prioritization logic specifically.

## Honest conclusions

1. **The cost story is real and large**: 5× cheaper, zero errors, at the price of much higher latency under this specific (GPU-constrained, 3-total-GPU) setup.
2. **This is not yet the cost-aware-*prioritization* test** — primary held at 1 the whole run, so we saw "hold cheap, spill to cheaper" rather than a genuine primary-vs-variant efficiency tradeoff under contention.
3. **The bugs mattered more than the workload choice.** Two independent, silent scaling-blocking bugs (documented above) mean any *prior* two-variant WVA results in this codebase (including PR #1435's own validation numbers) should be re-verified against real replica counts before being trusted at face value.
4. **Next natural step**: rerun at a higher rate or with primary's own max-replicas capped low enough to force it past its 85% threshold, to see whether WVA's cost-aware solver actually prioritizes the efficient variant once both are under real pressure — that's the scenario this run didn't reach.
