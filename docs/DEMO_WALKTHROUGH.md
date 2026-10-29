# Kubernetes Doctor — Five-minute technical demonstration

## Five-minute walkthrough

**Audience:** Platform, Kubernetes, and GPU infrastructure engineers. **Premise:** The scenario is entirely synthetic, inspired by the challenges of operating a multi-region, low-latency GPU inference platform. No third-party production clusters or metrics were accessed.

**0:00–0:40 — Context**

> I built Kubernetes Doctor because `kubectl describe` shows individual symptoms, but an incident often spans Ingress, Services, EndpointSlices, Pods, nodes and accelerators. My goal is to turn read-only Kubernetes topology and trustworthy telemetry into an evidence-backed troubleshooting graph. It's a local prototype, and I would validate it in a disposable cluster before production.

**0:40–1:35 — Visual topology**

Run `./scripts/demo.sh` before the meeting. Open `demo-output/reactor-dashboard.html`, scroll to **Discovered topology**. Trace Ingress → Service → Pod → Node, and Pod → PVC. Point out `selects` versus `routes_to`: selector match **does not guarantee** ready endpoints. On the failing Service there should be no `routes_to` edge, despite selected Pods.

**1:35–2:40 — Real-time inference incident**

On the dashboard, find `Service/inference/model-server` in **reactor-demo-us-west**. The selected Pod is in CrashLoopBackOff and was OOMKilled. Its assigned Node is NotReady, and an **external fixture** indicates GPU Xid errors. Another replica is Pending with insufficient eligible GPU scheduling capacity. The Service has no ready EndpointSlice backend. Synthetic latency and disconnect telemetry show possible customer impact. The root-cause hypothesis points to the GPU node **but does not prove** that its Xid caused the application's OOM. An operator should corroborate logs/timestamps, GPU metrics and readiness before intervention.

**2:40–3:30 — Fleet placement / multi-cloud thinking**

Run `kdoctor fleet --snapshot examples/reactor-demo/us-west.json --snapshot examples/reactor-demo/us-east.json --snapshot examples/reactor-demo/eu-central.json`. One regional cluster has an idle 8-GPU node, so the scheduler **capacity-only** screen can flag a candidate for the pending 8-GPU Pod. Explain why this is **not** automatic migration: model prewarming, GPU type, data residency, session continuity, checkpoint compatibility, GPU reservations and regional latency all matter. A real-world solution would route *new sessions* to healthy prewarmed replicas, and preserve existing sessions only if the platform's application protocol supports state restore.

**3:30–4:15 — Distributed training diagnosis**

The `reactor-demo-us-east` snapshot contains a synthetic NCCL timeout on one trainer rank and elevated RDMA retries/PFC pause on a peer node. Doctor connects them as co-occurring evidence, not definite network causality. A next-level integration would compare rank-aligned timestamps, ring/tree topology, NIC retransmits, GPU stalls and switch counters from existing observability tools.

**4:15–5:00 — Safe remediation**

Run `kdoctor plan --snapshot examples/reactor-demo/us-west.json`. Plans are read-only investigations. Real changes require a separate `kdoctor fix --apply --yes --policy ... --ticket ...` with allowlisted exact targets, Kubernetes SelfSubjectAccessReview, preconditions, audit logs and patch-state post-check. Cordon only an unhealthy node; uncordon only after recovery and separate approval. It never automatically deletes Pods, drains GPUs or moves active sessions. The honest design tradeoff: **fast diagnosis, conservative action**.

## Expected conversation topics / precise answers

**Why not an operator/controller?** The first version is a CLI because it avoids long-lived credentials and mutation loops. At scale I'd split collectors from a stateless correlation service, use informers/watch over repeated full scans, maintain topology with event timestamps, and use an independently authorized reconciliation/approval controller for narrowly typed remediations.

**How would you debug `Pending` GPU Pods?** Start with the PodScheduled event, compare GPU resource requests against per-node allocatable minus requested (not actual GPU utilization); then inspect node selectors, taints and tolerations, topology spread, RuntimeClass, device-plugin DaemonSet and DRA ResourceSlices if enabled. Don't assume `nvidia.com/gpu` scarcity means hardware is broken.

**Why can Services show no endpoints when matching Pods exist?** Selector match only selects candidates. Readiness and terminating state determine EndpointSlice usability. Compare selector, Pod readiness probes, targetPorts and EndpointSlice conditions. This tool currently models endpoints, but does not probe actual network connectivity.

**How do you recover real-time sessions if GPU nodes fail?** Workload continuity is an application-level problem. Kubernetes rescheduling is insufficient: have new sessions routed to healthy replicas, use health-aware admission and region-aware routing, and design state snapshots/checkpoint reload where model supports it. There are nonzero recovery latencies; avoid promising seamless migration without persisted state and compatible model/runtime.

**What about GPUs across many providers?** Normalize node labels, GPU resources, accelerator types, device plugin/DRA health and telemetry, but retain provider-specific capabilities (NVIDIA MIG, EFA, RDMA, topology, drivers). Use per-fleet schedulability filters before scoring available candidates for locality, latency, price and SLA.

**How do you avoid making things worse?** Read-only default; scoped RBAC; immutable evidence logs; deny-by-default change policy; admission checks; optional approval ticket; post-change SLO verification; and separate, tested rollback for reversible mutations. For stateful sessions, no generic Pod delete or drain.

## Clearly distinguish what exists from future work

**Implemented and tested offline:** Kubernetes resource collector/projection, topology graph, rules, synthetic signals, Prometheus/DCGM query adapter unit tests, correlated hypotheses, multi-context workflow, GPU capacity-only placement screen, offline dashboard, remediation policy and mocked API tests.

**Not validated live or built:** actual cluster integration/End-to-end GPU telemetry, native adapters for NCCL Doctor/Stallscope/Datapath Doctor, controller/operator reconciliation, true application SLO ingestion, stateful session migration, checkpointing, cross-region routing, guaranteed automated repair, rollout-health verification and automatic rollback.

## Discussion question

> In a globally distributed GPU fleet serving interactive inference workloads, which failure modes cost the most engineering time: GPU scheduling and capacity fragmentation, session loss during node degradation, or correlating model latency with Kubernetes and fabric signals?
