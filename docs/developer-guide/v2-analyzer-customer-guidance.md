# Choosing the Right Saturation Analyzer for Your Workload

The Workload Variant Autoscaler (WVA) ships with two saturation analyzers. Both
analyzers share the same scaling infrastructure and safety guardrails — the
difference is in how each one measures demand and decides when to add or remove
replicas.

---

## How Each Analyzer Works

**Standard analyzer (V1)** monitors the KV-cache occupancy percentage across
active decode replicas. When the average utilization crosses a configurable
threshold, it adds one replica at a time. This is a well-understood,
conservative approach that works reliably under stable, predictable traffic.

**Token-demand analyzer (V2)** measures demand in absolute token counts: tokens
currently occupying KV cache, tokens waiting in each pod's request queue, and
tokens queued at the inference gateway (EPP). It computes how many replicas are
needed to serve that total demand at the target utilization level, and scales
directly to that number in a single decision. This gives V2 the ability to
respond proportionally to traffic surges rather than incrementally.

---

## When the Token-Demand Analyzer Delivers the Most Value

### 1. Ramp-Up and Bursty Traffic Patterns

When request rates increase rapidly — for example, morning traffic spikes, batch
job bursts, or step-function load increases — requests begin queuing before the
system has had time to scale. The token-demand analyzer sees the growing queue
as unserved capacity demand and provisions the right number of replicas in a
single reconcile cycle, rather than adding replicas one by one as the queue
grows.

**Measured result** (prefill-heavy workload, TP=1, 2→6→10 RPS ramp-up):

| Metric | Standard | Token-Demand |
| --- | --- | --- |
| Avg TTFT | 107,930 ms | **12,444 ms** |
| P50 TTFT | 111,905 ms | **319 ms** |
| P95 TTFT | 250,833 ms | **61,530 ms** |
| P99 TTFT | 274,086 ms | **81,185 ms** |
| Avg TPOT | 29.67 ms/tok | **22.93 ms/tok** |
| P50 TPOT | 31.77 ms/tok | **20.68 ms/tok** |
| GPU time (15 min run) | 34.3 GPU·min | 73.8 GPU·min |

Under a 3× rate step (2 → 6 RPS), the median time to first token dropped from
**112 seconds to under one third of a second** — a 350× improvement. The average
improved by 8.7×. The 6 RPS stage is precisely the scenario where incremental
scaling leaves a growing backlog; the token-demand analyzer eliminates that
backlog by scaling to the required capacity immediately.

### 2. Prefill-Heavy Inference (Long Input Sequences)

Workloads with long prompts — summarization, document Q&A, retrieval-augmented
generation — fill KV cache quickly and queue up readily under load. Because the
token-demand analyzer accounts for queued tokens directly, it is especially
well-suited to these workloads: it sees demand building before KV cache
percentage alone would trigger a scale-out.

### 3. Latency-SLO-Sensitive Applications

When you have a TTFT or end-to-end latency SLO to protect, the difference
between a median TTFT of 112 s and 0.32 s is the difference between breaking
and meeting that SLO for the majority of users. The token-demand analyzer keeps
the median user experience fast even when the system is under load, at the cost
of provisioning additional GPU capacity during surge periods.

### 4. Variable or Unpredictable Traffic

Production inference traffic is rarely constant. If your workload has intra-day
variation, weekend peaks, or irregular spikes you cannot predict precisely, the
token-demand analyzer's ability to respond proportionally to observed queue
depth provides a margin of safety that constant-rate sizing cannot.

---

## Trade-offs to Consider

The token-demand analyzer provisions capacity proactively. During traffic
surges, it will use more GPU·minutes than the standard analyzer for the same
request volume. In the experiment above, the token-demand analyzer consumed
**73.8 GPU·min** over the 15-minute run compared to **34.3 GPU·min** for the
standard analyzer — a **2.15× increase in GPU time** in exchange for an **8.7×
improvement in average TTFT** and a **350× improvement in median TTFT**. Whether
that trade-off is favourable depends on the relative cost of GPU capacity versus
the business impact of latency degradation during traffic surges.

If your traffic is consistently steady and predictable, the standard analyzer's
conservative scaling may result in lower GPU spend with equivalent service quality.

Additionally, V2's tail TPOT (P95, P99) can be slightly higher during the
scale-out phase when many new replicas are prefilling concurrently. In the
experiment, P95 TPOT was 47.22 ms/tok (V2) vs 37.83 ms/tok (V1). This is a
transient effect as newly started pods handle their first batch of long prompts;
once replicas reach steady state, TPOT normalizes.

| Scenario | Recommended Analyzer |
| --- | --- |
| Ramp-up, bursty, or unpredictable traffic | **Token-demand (V2)** |
| Prefill-heavy workloads (long prompts) | **Token-demand (V2)** |
| Latency SLO must hold through traffic spikes | **Token-demand (V2)** |
| Steady, predictable traffic — cost is the primary constraint | Standard (V1) |
| Constant-concurrency load with no intra-period variation | Standard (V1) |

---

## Experiment Details

**Hardware**: single-variant TP=1 deployment, NVIDIA H100 80 GB, Llama 3.1 8B.  
**Workload**: prefill-heavy synthetic traffic (4 000 input / 1 000 output tokens,
Poisson arrivals), three rate stages of 5 minutes each: 2 → 6 → 10 RPS.  
**Protocol**: each run started from a fully clean state (KV cache flushed,
controller state reset) to eliminate cross-run contamination.  
**Replica cap**: 10. Both runs used identical HPA stabilization windows
(scaleUp: 0 s, scaleDown: 120 s) and identical threshold configuration.
