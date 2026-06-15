# Two-Variant WVA Benchmark Guide

Runs a controlled benchmark against a deployed two-variant WVA stack and
produces a per-reconcile decision table and pipeline graph.

## What you need

| Requirement | Details |
|---|---|
| Cluster access | `oc login` or `kubectl` pointing at pokprod001 |
| Namespace | `biran` (or your own two-variant deployment) |
| Fork branch | `feat/two-variant-wva-benchmark` of `github.com/biranofer/workload-variant-autoscaler` |
| Two-variant stack | Already deployed (primary TP=2 + v2 TP=1, WVA, HPAs) |
| llm-d-benchmark | Run `make benchmark-setup BENCHMARK_NAMESPACE=<ns>` once to clone and install |
| Python packages | `pip install matplotlib pyyaml` |

## Scenario

The default scenario (`test/benchmark/scenarios/prefill_heavy.yaml`) runs
three phases with a **closed-loop concurrent** profile:

```yaml
profile: concurrent
rate: [25, 100, 200]   # concurrent in-flight requests per phase
max_seconds: 300        # 5 min per phase = 15 min total
data:
  prompt_tokens: 4000
  output_tokens: 1000
```

`profile: concurrent` keeps exactly N requests in-flight at all times
(closed-loop). This causes saturation **during** the benchmark window at
rate=100/200, with zero post-benchmark drain spike. Edit the scenario file
to change rates or durations.

## Running the benchmark

```bash
cd /path/to/workload-variant-autoscaler
git checkout feat/two-variant-wva-benchmark

./hack/benchmark/run_wva_benchmark.sh -n biran
```

The script handles everything automatically:
1. Stops WVA and verifies pod deletion
2. Scales model pods to 1/1 with no WVA competing
3. Starts a fresh WVA pod (zero k2 history)
4. Begins in-flight WVA log capture
5. Verifies first reconcile shows P4 (confirmed clean history)
6. Launches the benchmark harness
7. Kills the harness after metrics are collected (skips slow report conversion)
8. Copies metrics from PVC and runs post-processing

### Options

```
-n NAMESPACE    Kubernetes namespace (required)
-m MODEL_ID     Model ID (default: unsloth/Meta-Llama-3.1-8B)
-s SPEC         llmdbenchmark spec (default: guides/two-variant-wva)
-w WORKLOAD     Scenario file (default: prefill_heavy.yaml)
```

### Expected output

```
[10:00:00Z] Step 1: Stopping WVA controller...
[10:00:15Z] WVA pod deleted
[10:00:20Z] Step 2: Scaling model pods to 1/1 (no WVA competition)...
[10:00:45Z] Step 3: Starting fresh WVA controller...
[10:00:47Z] New WVA pod: workload-variant-autoscaler-...-xxxxx
[10:00:47Z] Step 4: Starting in-flight WVA log capture -> /tmp/wva-debug-run.log
[10:01:22Z] Step 5: Verifying clean k2 history...
[10:01:22Z] VERIFIED: P4 (fallback-k1) — zero k2 history. Clean start confirmed.
[10:01:22Z] Step 6: Launching benchmark...
...
[10:17:00Z] Step 7: Harness completed. Killing pod...
[10:17:10Z] Step 8: Copying metrics from PVC...
[10:17:30Z] Step 9: Running post-processing...
[10:17:35Z] Done. Results:
[10:17:35Z]   Table:  biran-20260614-.../results/.../metrics/processed/wva_decision_table.txt
[10:17:35Z]   Graph:  biran-20260614-.../results/.../metrics/graphs/two_variant_v2_full_pipeline.png
```

## Reading the decision table

```
Time          K1_pri    K1_v2    K2_pri    K2_v2  K2src_pri   K2src_v2      Demand   Util    Eff_pri     Eff_v2  Decision
17:38:28Z    751,820  329,574   751,820  329,574      P4-k1      P4-k1           0     0%   1.33e-05   1.52e-05  NC
...
17:49:59Z    751,820  329,574  1,152,000   51,400   P3-deriv    P2-hist     557,592    69%   1.33e-05   9.73e-05  NC
17:54:59Z    751,820  329,574  1,152,000  403,391   P3-deriv     P1-obs   1,041,047    96%   1.33e-05   1.52e-05  P→2
17:55:29Z    751,820  329,574   294,740  403,391     P1-obs     P1-obs   1,375,671   220%   3.39e-05   1.52e-05  V2→4
```

| Column | Meaning |
|---|---|
| `K1_pri/v2` | Memory-bound capacity (KV cache limit × 0.80) |
| `K2_pri/v2` | Compute-bound capacity used by WVA this cycle |
| `K2src_pri/v2` | How k2 was computed: P1=observed, P2=history, P3=derived, P4=fallback-k1 |
| `Demand` | Total token demand (KV in-use + waiting queue + EPP queue) |
| `Util` | `totalDemand / totalSupply` — can exceed 100% when queue > 0 |
| `Eff_pri/v2` | Cost-efficiency = `cost / perReplicaCapacity` — **lower is more efficient** |
| `Decision` | Scale action: `P→2` = primary scaled to 2, `V2→3` = v2 scaled to 3, `NC` = no change |

### What to look for

**Clean start (P4):** First rows should show `K2src=P4-k1` for both variants —
confirms zero k2 history from previous runs.

**Correct scale ordering:** When util first exceeds 85%, the variant with
lower `Eff` should be selected. With clean history:
- Primary eff ≈ 1.33e-05 (cost=10, k2=751K)
- v2 eff ≈ 1.52e-05 (cost=5, k2=329K)
- **Primary is more efficient → `P→2` should fire first** ✓

**P1 contamination:** When P1 fires on primary under saturation, k2_pri drops
(e.g. 294K) making primary appear less efficient → subsequent scale-ups may
choose v2. This is expected under heavy load — the k2 history issue.

**Clean end:** After benchmark ends, demand drops immediately to 0 (concurrent
profile has no post-benchmark drain). If demand spikes after benchmark ends,
switch back to `profile: constant`.

## Interpreting the graph

The pipeline graph has 5 panels:

1. **Replica Count** — solid lines = actual ready pods, dashed = WVA desired
2. **Estimated Demand vs Capacity** — stacked bars (KV in-use / waiting / EPP
   queue) vs raw KV capacity line
3. **KV Cache Utilization** — per-variant KV % from Prometheus
4. **Requests Running** — vLLM `num_requests_running` per variant
5. **vLLM Waiting** — `num_requests_waiting` per variant

## Troubleshooting

**"Did not see P4 in first reconcile":** The WVA pod was not freshly started.
This can happen if the stop/wait failed. Re-run the script — it will try again.

**`oc login` expired:** Run `oc login https://api.pokprod001...` and retry.

**Harness pod not starting:** Check `kubectl get pod -n biran | grep guidellm`
and inspect logs. Often a prior harness pod needs cleanup:
`kubectl delete pod -n biran -l app=llmdbench-harness-launcher`

**capacityRaw=0 in graph (no blue bars):** The regex for `vllm:cache_config_info`
failed — this is a known issue when `mamba_block_size="None"` is present in the
metric labels. The fix is already applied in `dump_capacity_demand_estimate.py`
on this branch.

**WVA log too short / table has few rows:** The pod log buffer rotated during a
long run. The script captures logs in-flight (`/tmp/wva-debug-run.log`), so
this should not happen if the script is used as-is.
