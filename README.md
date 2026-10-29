# Kubernetes Doctor · `kdoctor`

**Topology → evidence → root-cause hypotheses → optional operator-controlled remediation.**

An open-source Kubernetes incident investigation CLI and self-contained HTML dashboard for multi-cluster GPU infrastructure. All sample incidents are **synthetic**, including the Reactor-named example fixtures. This project is **not affiliated with Reactor** and has never accessed or been tested on its infrastructure.

**Status (v0.2):** offline fixtures, telemetry contracts, correlation and unit tests implemented. Live Kubernetes API integration, Prometheus access and actual cluster mutations have **not** been exercised against a disposable or production cluster. This is **not** a production-ready autonomous repair controller or published PyPI package.

## Five-minute demo

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install pytest
pytest -q
./scripts/demo.sh
```

Open **`demo-output/reactor-dashboard.html`** in your browser. The demo does **not contact Kubernetes** or make any cluster changes.

See the [five-minute demo walkthrough](docs/DEMO_WALKTHROUGH.md) for expected findings and limitations. A [pre-rendered synthetic dashboard](docs/synthetic-demo-dashboard.html) is also included for offline inspection.

## CLI

```bash
# Real Kubernetes API (read-only with kubeconfig or in-cluster service account):
kdoctor snapshot -o inventory.json
kdoctor scan --format json -o triage.json
kdoctor explain
kdoctor topology --format mermaid -o dependency.mmd

# Offline snapshots: no kubeconfig required
kdoctor explain --snapshot examples/reactor-demo/us-west.json
kdoctor topology --snapshot examples/reactor-demo/us-west.json --format json
kdoctor plan --snapshot examples/reactor-demo/us-west.json

# Fleet: repeat --snapshot or --context; --all-contexts supported
kdoctor fleet --snapshot examples/reactor-demo/us-west.json \
              --snapshot examples/reactor-demo/us-east.json \
              --snapshot examples/reactor-demo/eu-central.json
kdoctor report --snapshot examples/reactor-demo/us-west.json \
               --snapshot examples/reactor-demo/us-east.json \
               --snapshot examples/reactor-demo/eu-central.json \
               -o report.html

# Live multiple kubeconfig contexts
kdoctor fleet --context cluster-a --context cluster-b --format json -o fleet.json
kdoctor report --all-contexts -o all-clusters.html

# Optionally add source-labelled external telemetry
kdoctor explain --snapshot examples/reactor-demo/eu-central.json \
                --signals examples/reactor-demo/dcgm-sample.json

# Optional read-only DCGM signals from Prometheus: no auth support yet
kdoctor scan --prometheus https://prometheus.example.internal
```

`scan` and `explain` exit 1 when findings are present, 2 for incomplete collection, 3 for execution errors; `fleet`/`report` exit 2 on incomplete inventory. `topology`, `snapshot` and `plan` return 0 after a complete output. Snapshots are timestamped inventory samples, **not atomic cross-kind snapshots**.

## Architecture

```text
            Cluster A          Cluster B        Cluster C
            Kubernetes API     Kubernetes API   Kubernetes API
                  \               |                /
                   Curated read-only Kubernetes collector
                          | (JSON inventory)
             ┌────────────┼─────────────────┐
             |            |                 |
        Dependency     Rule engine      Telemetry adapters
        graph          + events          (JSON / Prometheus-DCGM)
             |            |                 |
             └────────────┼─────────────────┘
                          |
                 Fault-tree correlation
                     (hypotheses)
                          |
            Fleet analysis + GPU capacity screening
                          |
             CLI / JSON / Mermaid / offline HTML
                          |
               Human-authored recovery decision
                          |
              Deny-by-default approval policy
                   /                  \
            guarded restart     cordon / uncordon
                     local audit + API verification
```

### Resource discovery

The Kubernetes Python client lists Nodes, Pods, Services, EndpointSlices, Deployments, ReplicaSets, PVCs, PVs, Ingresses, NetworkPolicies and Events. It projects **selected fields** into JSON instead of copying Secret objects, Pod environment variables or full Pod specs. Errors and resource truncation are explicitly reported; missing RBAC must not be mistaken for a healthy cluster. `deploy/rbac-readonly.yaml` grants read-only permissions for these kinds.

Topology relations include `Ingress → Service`, `Service → selected Pod`, `Service → ready EndpointSlice target`, `Deployment → ReplicaSet → Pod`, `Node → Pod`, `Pod → PVC → PV`, `Pod → node GPU resource`, and `NetworkPolicy → selected Pod`. The `selects` edge is **not** proof traffic is routed; only a `routes_to` edge denotes a ready/nonterminating EndpointSlice backend. Physical fabric and inter-region connectivity are not directly discovered.

### Diagnostic rules and correlation

Included conditions: node NotReady/pressure/cordoned; Pod unschedulable and GPU resource constraints; Pending, CrashLoopBackOff, ImagePullBackOff, OOMKilled; missing ready Service backends; PVC unbound; Deployment unavailable; Ingress backend failures; CNI sandbox and DNS **event indicators**. Telemetry-driven findings include GPU Xid/ECC, NCCL timeouts, RDMA retransmissions, PFC pauses, packet drops, inference p99 frame latency, session disconnects and GPU memory pressure.

The fault-tree engine correlates known ownership/selection relationships and ranks **hypotheses**, not probabilities or ground-truth root causes. A rule and supporting evidence are always included; root cause remains subject to manual verification. A distributed training timeout can be connected to peer-node fabric signals when the Pods share an app label; this correlation is **not evidence that the RDMA event caused the timeout**.

### Telemetry contract

External signals are **not read from unrelated projects automatically**. A producer or export adapter must map its output into this schema:

```json
{
  "signals": [
    {
      "source": "dcgm",
      "metric": "gpu_xid_errors",
      "value": 1,
      "resource": "Node/gpu-west-a",
      "observed_at": "2026-10-28T20:12:00Z"
    }
  ]
}
```

Allowed metrics and demo threshold defaults live in `src/kubernetes_doctor/signals.py`. All values and thresholds are subject to deployment-specific tuning. `--signals PATH` imports curated JSON; `--prometheus URL` queries two NVIDIA DCGM metric expressions over the `/api/v1/query` API if available (`DCGM_FI_DEV_XID_ERRORS`, `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`). Prometheus queries are **optional**, read-only, currently lack auth/TLS-client-cert configuration and are not guaranteed available in every exporter. Failures mark the inventory incomplete. More sophisticated adapters for NCCL Doctor / Stallscope / Datapath Doctor are **integration seams**, not shipped native integrations.

### Multi-cluster capacity screen

`fleet` inventories each context independently and tallies **Pod GPU requests**, Node GPU allocatable quantities and unclaimed GPUs. It flags a possible target when a pending GPU Pod could fit on an untainted, Ready node in another cluster *by that one GPU-resource quantity alone*. A `CAPACITY_ONLY_UNVERIFIED` candidate is **not a relocation recommendation**, and the tool never transfers workloads or customer sessions. Proper admission also requires model/driver compatibility, GPU profiles, reservations, topology/affinity, tenant isolation, location, storage, warm model capacity, latency, and session/checkpoint transfer semantics.

## Remediation: only with explicit consent

Default behavior is read-only. `kdoctor plan` proposes human-run investigations, never mutates.

A few narrowly typed actions are available separately, each initially in **dry-run** mode:

```bash
# Requires a healthy, unpaused Deployment with >=2 replicas, all available/ready/updated.
kdoctor fix --context staging --namespace sandbox --deployment demo-api --dry-run

# With a real cluster and an allowlisted policy, ticket, RBAC authorization and confirmation:
kdoctor fix --context staging --namespace sandbox --deployment demo-api \
  --apply --yes --policy /secure/path/restart-policy.json --ticket CHG-1929

# Cordon a *NotReady* node (does not drain/move existing workloads):
kdoctor fix --context staging --action cordon-node --node gpu-node-1 --dry-run

# Explicit reverse operation is allowed only when the node is Ready and pressure-free:
kdoctor fix --context staging --action uncordon-node --node gpu-node-1 --dry-run
```

`examples/reactor-demo/restart-policy.json` is **only an example** of an allowlist and does not grant RBAC permission or match production contexts. Apply requires `--apply --yes --policy FILE --ticket ID`. SelfSubjectAccessReview must allow `patch` on that exact resource. The CLI writes a local JSONL audit record on attempted/verified patches. API post-read checks verify only the **patch state**; they do not prove restored service health, a completed rollout, or disruption-free operation. Node scheduling changes are **reversible via a separate explicitly approved action**, not automatic rollback. Deployment rollout restarts cannot be safely undone by restoring the old timestamp. Never use `--apply` as a blanket response to a finding.

The read-only RBAC file intentionally provides **no write access**. Provision tightly scoped, separate write RBAC for the exact resources/actions you approve. Workloads with a single replica, budgets that permit zero disruption, or critical customer sessions need a different recovery process.

## Deploy and package

- `python -m pip install .` builds and installs locally with Python 3.10+; `kdoctor` is the CLI.
- `python -m pip install -e .` enables editable source iteration.
- `python -m build` creates a wheel and source distribution if the `build` package is installed.
- `Dockerfile` builds a non-root runtime image. Change the image placeholder in `deploy/job.yaml`; use `kubectl apply -f deploy/rbac-readonly.yaml` first if you choose to run in-cluster.
- The package is distributed here as **`kubernetes-doctor-mvp`**. We have **not** published `kubernetes-doctor` on PyPI or verified trademark/package-name availability.

## Verification and security boundaries

Run `pytest -q` for deterministic unit, fixture, CLI, HTML injection-escaping, policy and mock-API tests. More work is required before production: live integration in Kind/K3s, API throttling, authentication/impersonation, async collectors/informers, RBAC hardening, Kubernetes compatibility matrix, network-policy evaluation, telemetry time-alignment, driver health, DRA support, workload SLA integration, multi-tenancy, rollout verification and persistent auditing. Avoid granting cross-cluster administrator credentials to a background daemon. Events can contain internal addresses and other sensitive details even though Secrets and Pod env are excluded; restrict access to snapshots and reports.

## Relevant documentation

- [Kubernetes client for Python](https://github.com/kubernetes-client/python)
- [EndpointSlice behavior](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/)
- [NVIDIA DCGM exporter](https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/latest/)
- [Kubernetes authorization reviews](https://kubernetes.io/docs/reference/access-authn-authz/authorization/)

Licensed under Apache-2.0. This tool is a prototype and has no affiliation with Kubernetes, NVIDIA or Reactor.
