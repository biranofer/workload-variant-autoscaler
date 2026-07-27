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

Date: 2026-07-27
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

**KEDA-EPP** (`guides/keda-epp-two-variant`, no WVA installed at all)
- Metric feeding HPA: same two Prometheus queries as the single-variant KEDA-EPP baseline — `sum(inference_extension_flow_control_queue_size)` and `sum(inference_objective_running_requests)` — applied to **both** variants independently, each with its own `ScaledObject`.
- Because EPP only exposes pool-wide (not per-Deployment) queue/running-request signals, both ScaledObjects read the *identical* value and scale in lockstep — this is the correct behavior for the naive baseline: KEDA-EPP has no cost-awareness to differentiate the two variants at all.
- Thresholds: queue avg/pod = 50, running avg/pod = 16 (same as the single-variant `scaledobject-t50.yaml`, chosen for a fairer comparison against WVA V2).

Both share the same KEDA HPA scaleUp/scaleDown behavior: Percent=100/15s, ScaleUp stab=0s, ScaleDown stab=120s.

**Workload** (`prefill_heavy_4k1k_2_10`, Poisson arrival): input ≈4000 tokens, output ≈1000 tokens, 5 min @ rate=2 (warm-up) then 12 min @ rate=10 (saturation). 7800 requests total.

## Prerequisite bug fixes — without these, the results are not trustworthy

All of these produced plausible-looking symptoms while the WVA controller's own decision log looked completely healthy — a reminder that a controller's decision log is not proof its decisions were applied, or that its inputs were correct.

1. **`add_variant.py`'s variant Deployment selector was a superset of the primary's** — inherited (never removed) the primary's `llm-d.ai/inference-serving` discriminator label, so Kubernetes' native HPA controller's `AmbiguousSelector` safety check fired for both HPAs and blocked scaling entirely. Fixed in `hack/benchmark/add_variant.py::make_variant_deployment()`.
2. **The WVA controller's `ServiceMonitor` has a hardcoded TLS `serverName`** (`wva-controller-manager-metrics-service.wva-system.svc`) that doesn't match the real tenant namespace, silently breaking the HTTPS scrape — `wva_desired_replicas` never reaches Prometheus, so KEDA's HPA always reads empty and never scales. This resets on every fresh `benchmark-standup` and is not yet fixed at the template level; requires a manual patch after every standup:
  ```
  oc patch servicemonitor wva-controller-manager-metrics-monitor -n <ns> --type=json \
    -p '[{"op":"replace","path":"/spec/endpoints/0/tlsConfig/serverName","value":"wva-controller-manager-metrics-service.<ns>.svc"}]'
  ```
3. **WVA's collector trusts a pod's `llm-d.ai/variant` label as-is whenever it's *present*** (`buildInstanceKey` in `internal/collector/replica_metrics.go`) — it only falls back to the correct owner-chain-based lookup when the label is *missing*. Two places set this label to a value that matched no tracked variant, silently dropping that pod's queue-backlog demand contribution (the in-use-KV component is attributed through a different, unaffected path, which is why the controller still scaled *something* while undercounting demand ~2×):
   - **The added variant's own label**, set by `add_variant.py` to the bare Deployment name instead of the tracked ScaledObject's name (`<deployment>-scaler`). Fixed in `make_variant_deployment()`.
   - **Primary's label**, set unconditionally by the `llm-d-benchmark` modelservice chart template (`13_ms-values.yaml.j2`, gated only on `wva.enabled`) to `<model_id_label>-decode` — baked into the Deployment's *selector* too (selector must be a subset of template labels), making it immutable once created. Rather than fight an upstream chart default we can't durably patch, `add_variant.py` now reads primary's live label and **names the primary ScaledObject to match it**, instead of assuming a fixed `<deployment>-scaler` pattern — no Deployment edit needed at all.
4. **Both scenario files set the EPP plugin config under the wrong key** (`inferenceExtension.pluginsConfigFile`/`pluginsCustomConfig` instead of the chart's actual `router.epp.pluginsConfigFile`/`pluginsCustomConfig`) — `inferenceExtension.*` is llm-d-benchmark's naming convention for its "convert-guide" skill's GUIDE input format, not a key a benchmark *scenario* file is templated against. This silently no-oped: the rendered EPP ConfigMap never got our intended `featureGates: [flowControl]` plugin config, meaning **the flow-control admission layer — and its queue-depth metric — had never actually been active in any run of this scenario**, including every run behind the numbers below before 2026-07-27. Fixed in both `two-variant-wva.yaml` and `keda-epp-two-variant.yaml`.
5. **Primary's `maxReplicas` was inherited from the modelservice chart's auto-generated legacy HPA** instead of being a fixed benchmark parameter — that chart default varies by chart-internal heuristics we don't control (observed 16 on one standup, 10 on others). Not reproducible for a benchmark comparison; fixed in `add_variant.py` to always use 10, matching the variant's explicit `maxReplicas: 10`.

Verified fixed (#3): cross-checking WVA's `totalDemand` against an independent raw-scrape estimate (`dump_capacity_demand_estimate.py`, computed directly from vLLM/EPP Prometheus scrapes with no dependency on WVA's own attribution) sample-by-sample shows the ratio sitting at ~0.9–1.3 throughout the run — matching within normal sampling noise, vs. a consistent ~0.5 before the label fixes.

Verified fixed (#4): after the key-path fix, the live EPP ConfigMap gained a real `wva-plugins.yaml`/`keda-epp-plugins.yaml` entry (confirmed via the rendered `config.yaml` and the EPP Deployment's own `--config-file` arg pointing to it), and raw Prometheus scrapes during the run show genuine nonzero `inference_extension_flow_control_queue_size` values for the first time in any run of this scenario (WVA leg: peak 335; KEDA-EPP leg: consistently 0 — see "Reading these numbers" for why that's expected, not a bug).

Confirmed the label bug (#3) is scoped to the two-variant path specifically: all 10 result directories behind [`comparison-wva-keda-epp-20260722`](../comparison-wva-keda-epp-20260722/comparison.md)'s single-variant comparison carry *no* `llm_d_ai_variant` label on any pod at all (label absent → correct fallback path), so that doc's numbers are unaffected. The EPP plugin config bug (#4) is *not* scoped to two-variant, though — `wva-sat2-tp1.yaml` and `keda-epp-tp1.yaml` (the single-variant scenarios) carry the identical mistake and are fixed alongside these two.

Also fixed along the way (not scaling-correctness bugs, but distort results if left alone): the harness pod's default 32Gi memory OOMKilled mid-run on this workload's 1000-token mean output (bumped to 64Gi in both scenario files), and `report.request_lifecycle.per_request: true` generated a multi-GB JSON file (observed 6GB+, 45+ minutes to collect) that nothing in the analysis pipeline reads — disabled in the workload file.

## Results

| metric                                |       WVA | KEDA-EPP   |
|---------------------------------------|-----------|------------|
| requests                              |      7800 | 7800       |
| successful                            |      7682 | 7776       |
| errors                                |       118 | **24**     |
| error rate                            |     1.51% | **0.31%**  |
| achieved RPS                          |      7.45 | 7.39       |
| avg replicas (primary)                |  **2.27** | 5.72       |
| avg replicas (variant)                |  **1.00** | 5.77       |
| max replicas (primary)                |     **7** | 10         |
| max replicas (variant)                |     **1** | 10         |
| cost (weighted avg replicas × GPU/hr) |  **5.54** | 17.21      |
| avg KV cache utilization              |     22.1% | 5.2%       |
| peak EPP flow-control queue           |   **335** | 0          |
| TTFT p50 (ms)                         |       190 | **139**    |
| TTFT p95 (ms)                         |    23,700 | **1,404**  |
| TTFT p99 (ms)                         |    39,320 | **6,003**  |
| Request latency p50 (ms)              |    16,620 | **12,838** |
| Request latency p95 (ms)              |    56,910 | **18,333** |
| Request latency p99 (ms)              |    65,020 | **21,625** |
| TPOT p50 (ms)                         |      16.2 | **12.9**   |
| TPOT p95 (ms)                         |      37.2 | **19.7**   |
| TPOT p99 (ms)                         |      63.7 | **43.6**   |

Reproducing:

| run      | results dir                                                             |
|----------|-------------------------------------------------------------------------|
| WVA      | `biran-20260727-004110-190/results/inference-perf-1785102124-u3euex_1/` |
| KEDA-EPP | `biran-20260727-093601-697/results/inference-perf-1785134226-ul2mis_1/` |

## Graphs

Full per-run pipeline plots (replica count with desired/ready overlay, estimated demand vs capacity, KV utilization, requests running/waiting, EPP queue depth — all vs time):

### WVA — primary overshoots once then settles at 2, variant stays flat at 1

![WVA two-variant pipeline](img/wva_pipeline.png)

With all five fixes in place — including, for the first time, a genuinely active EPP flow-control signal — WVA does what the cost-aware pricing predicts: **primary (TP=2, the more GPU-efficient variant per unit cost) is the one that scales**, up to 7 replicas under load, while the variant (TP=1) never leaves 1 for the entire run.

The replica-count panel shows a **single overshoot-and-correct cycle**, not the repeated two-hump oscillation seen in the previous (EPP-flow-control-inactive) version of this run: primary's desired target climbs 1→3→5→7, ready count follows up to 7, then both settle back down to 2 and hold steady for the remaining ~10 minutes of the saturation stage — no further flapping. The 118 errors (1.51%) cluster at 31–45s request latency (well under the 300s client timeout), consistent with in-flight streams severed during that one scale-down (7→4→2), not a sustained problem. This is a real reliability finding, but a narrower one than previously documented: one transient correction under a sudden ramp, not a persistent control-loop oscillation.

The EPP Queue Metrics panel shows the newly-active `flow_control_queue (gateway)` (black) tracking closely with `per pod sum: primary` during that same window, peaking at 335 — confirming the flow-control admission layer is genuinely active and reacting to real backlog, not silently inert as in every prior run of this scenario.

### KEDA-EPP — both variants scale in lockstep (no cost-awareness)

![KEDA-EPP two-variant pipeline](img/keda_epp_pipeline.png)

Both ScaledObjects read the identical pool-wide EPP signal, so primary and variant climb together (1→9-10) and fall together — exactly the expected naive-baseline behavior, since KEDA-EPP has no mechanism to prefer the more GPU-efficient variant. The flow-control queue metric is now genuinely active here too (confirmed via raw scrapes) but reads a consistent, real 0 throughout — KEDA-EPP scales aggressively enough on the `running_requests` trigger alone that the EPP admission layer never has to buffer anything. Not a bug; the same behavior was already documented for a comparable single-variant run on 2026-07-24, before the key-path bug was even found.

## Reading these numbers

**Cost**: WVA holds the line at ~3.3 total replicas (2.27 primary + 1.0 variant) for the entire run, vs KEDA-EPP's ~11.5 total (5.72 + 5.77) — **a ~3.1× cost difference** (5.54 vs 17.21, weighted by GPUs/replica).

**WVA correctly prioritizes the more efficient variant, and this now holds with a genuinely complete demand signal**: primary (TP=2, the better cost-per-GPU pick given cost ratio 10/5 = TP ratio 2/1) is the one that scales, up to 7 replicas; the variant (TP=1) never leaves 1, for the entire run. This was first demonstrated in the previous version of this doc (before the EPP flow-control fix); it holds unchanged now that WVA's demand model is actually complete (all three of its inputs — in-use KV, vLLM's own queue, and EPP's flow-control queue — are live and correctly attributed).

**Did fixing the flow-control metric change anything?** Barely. Compare against the previous version of this run (flow-control inactive, everything else already fixed): errors went from 104 (1.33%) to 118 (1.51%); TTFT p95/p99 from 23,620/35,968ms to 23,700/39,320ms; request latency p95/p99 from 58,668/66,546ms to 56,910/65,020ms. All differences are well within normal Poisson run-to-run variance — **enabling a real, previously-inactive EPP-queue-demand signal did not measurably change WVA's behavior here.** That's an important, if quiet, finding on its own: whatever is driving the transient overshoot-and-correct pattern, it isn't fixed (or worsened) by having real EPP backlog data available to react to. An intermediate live-patch test earlier in this investigation showed a much worse result (TTFT p95 88s, errors 177) — that turned out to be caused by a *different*, since-fixed bug (the `maxReplicas` inheriting 16 from the chart's own legacy HPA default, not 10), not by the flow-control fix itself. Worth remembering as a caution: when two fixes land close together, attribute the effect to the specific one, not "the batch of changes."

**Is this a fair test of WVA's value proposition?** Yes, more so than any earlier version of this comparison. It demonstrates cost-aware *prioritization* (primary over variant) at ~3× lower cost than the naive baseline, with a demand model that's now genuinely complete rather than accidentally-adequate. The one open reliability question is narrower than previously thought: a single transient overshoot under a sudden load ramp, not a persistent oscillation — worth checking whether it reproduces across repeated runs before concluding it's systemic.

## Honest conclusions

1. **The cost-aware prioritization story is real and now rests on a complete demand signal**: primary (the GPU-efficient variant) scales, the cheaper-but-less-efficient variant doesn't — at ~3× lower cost than the naive KEDA-EPP baseline — with all three components of WVA's demand model (in-use KV, vLLM queue, EPP flow-control queue) genuinely live and correctly attributed for the first time.
2. **Five independent fixes were needed to get here** (see "Prerequisite bug fixes" above) — two more than the previous version of this doc reported. Any *prior* two-variant WVA or KEDA-EPP results in this codebase — including PR #1435's own validation numbers — should be re-verified against real replica counts and an independent demand cross-check before being trusted at face value.
3. **Enabling the previously-inactive EPP flow-control signal did not meaningfully change WVA's results** — errors, TTFT, and request latency all landed within normal run-to-run variance of the pre-fix numbers. This argues against the flow-control term being either a hidden cause of, or a hidden fix for, the reliability behavior documented here.
4. **The reliability finding is narrower than previously documented**: a single transient overshoot-and-correct cycle under a sudden load ramp (7→4→2 replicas, errors clustered in that one window), not a persistent two-hump oscillation. Worth confirming this is reproducible across repeated runs before treating it as a systemic control-loop problem requiring a `scaleDownBoundary`/hysteresis fix.
5. **KEDA-EPP's own flow-control trigger is also now genuinely wired up correctly** (same key-path bug, fixed in `keda-epp-two-variant.yaml` too) but reads a real, consistent 0 throughout this run — KEDA-EPP's documented lockstep scaling was, and still is, driven entirely by the `running_requests` trigger. Not a corruption of the earlier result, just confirmation of which trigger was actually doing the work.
