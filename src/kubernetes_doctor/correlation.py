"""Rule-based, explicitly tentative fault-tree correlation. No generative claims of causality."""
from collections import defaultdict

SEVERITY = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _resource(kind, ns, name):
    return f"{kind}/{ns + '/' if ns else ''}{name}"


def correlate(snapshot, findings):
    """Return hypotheses with a human-auditable dependency path and supporting finding IDs."""
    by_resource = defaultdict(list)
    for finding in findings:
        by_resource[finding["resource"]].append(finding)
    pods = snapshot.get("pods", [])
    nodes = {n.get("name"): n for n in snapshot.get("nodes", [])}
    incidents = []

    def make(impact, impacted, hypotheses, score=None):
        # Ranking is deterministic and a heuristic, not a calibrated probability.
        ordered = sorted(hypotheses, key=lambda x: (-x["score"], x["resource"], x["rule"]))
        top = ordered[:4]
        if not top:
            return
        severity = "critical" if any(h["score"] >= 6 for h in top) else "high"
        incidents.append({
            "id": f"incident:{impacted}", "impact": impact, "resource": impacted,
            "severity": severity, "hypotheses": top,
            "primary_suspect": top[0]["resource"],
            "confidence": "correlated" if len(top[0]["path"]) > 1 else "signal-only",
            "caution": "Ranked hypotheses, not proven causality. Verify before changing workloads.",
        })

    def candidates(resource, path):
        output = []
        for finding in by_resource.get(resource, []):
            if finding["rule"] in ("NO_READY_ENDPOINTS", "DEPLOYMENT_UNAVAILABLE", "POD_PENDING"):
                continue
            output.append({"resource": resource, "rule": finding["rule"],
                           "finding_id": finding["id"], "reason": finding["summary"],
                           "evidence": finding.get("evidence", [])[:3],
                           "path": path, "score": SEVERITY.get(finding["severity"], 1) + len(path)})
        return output

    for svc in snapshot.get("services", []):
        ns, name = svc.get("namespace"), svc.get("name")
        impacted = _resource("Service", ns, name)
        if not any(f["rule"] == "NO_READY_ENDPOINTS" for f in by_resource[impacted]):
            continue
        selector = svc.get("selector") or {}
        matches = [p for p in pods if p.get("namespace") == ns and selector and
                   all(p.get("labels", {}).get(k) == v for k, v in selector.items())]
        hypotheses = []
        for pod in matches:
            pod_resource = _resource("Pod", ns, pod["name"])
            path = [impacted, pod_resource]
            hypotheses.extend(candidates(pod_resource, path))
            if pod.get("node") in nodes:
                node_resource = _resource("Node", "", pod["node"])
                hypotheses.extend(candidates(node_resource, path + [node_resource]))
        if not matches:
            hypotheses.append({"resource": impacted, "rule": "SELECTOR_MISMATCH_OR_NO_PODS",
                               "finding_id": next(f["id"] for f in by_resource[impacted] if f["rule"] == "NO_READY_ENDPOINTS"),
                               "reason": "No pods match the Service selector in the inventory",
                               "evidence": [f"selector={selector}"], "path": [impacted], "score": 3})
        if not hypotheses:
            # A service may have matching, healthy-looking pods but still lack endpoints.
            hypotheses.append({"resource": impacted, "rule": "ENDPOINTS_NOT_READY_UNEXPLAINED",
                               "finding_id": next(f["id"] for f in by_resource[impacted] if f["rule"] == "NO_READY_ENDPOINTS"),
                               "reason": "No ready EndpointSlice backend; inspect readiness and EndpointSlice controller",
                               "evidence": [], "path": [impacted], "score": 2})
        make("No ready endpoints; new connections cannot reach a ready backend", impacted, hypotheses)
        impact_signals = [f for f in by_resource[impacted] if f["rule"] in ("REALTIME_LATENCY_SLO", "SESSION_DISCONNECTS")]
        if impact_signals:
            incidents[-1]["impact_evidence"] = [{"rule": f["rule"], "evidence": f["evidence"]}
                                                 for f in impact_signals]

    # Trace availability failures via Deployment → ReplicaSet → Pod → Node.
    replicasets = { (r.get("namespace"), r.get("name")): r for r in snapshot.get("replica_sets", []) }
    for dep in snapshot.get("deployments", []):
        ns, name = dep.get("namespace"), dep.get("name")
        impacted = _resource("Deployment", ns, name)
        if not any(f["rule"] == "DEPLOYMENT_UNAVAILABLE" for f in by_resource[impacted]):
            continue
        hypotheses = []
        for pod in pods:
            if pod.get("namespace") != ns:
                continue
            owner = next((o for o in pod.get("owners", []) if o.get("kind") == "ReplicaSet"), None)
            rs = replicasets.get((ns, owner.get("name"))) if owner else None
            if not rs or not any(o.get("kind") == "Deployment" and o.get("name") == name for o in rs.get("owners", [])):
                continue
            path = [impacted, _resource("ReplicaSet", ns, rs["name"]), _resource("Pod", ns, pod["name"])]
            hypotheses.extend(candidates(path[-1], path))
            if pod.get("node") in nodes:
                res = _resource("Node", "", pod["node"])
                hypotheses.extend(candidates(res, path + [res]))
        make("Deployment has insufficient available replicas", impacted, hypotheses)

    # A pending GPU Pod is an impact in its own right; resource topology is insufficient to prove why.
    for pod in pods:
        res = _resource("Pod", pod.get("namespace"), pod.get("name"))
        gpu = [f for f in by_resource[res] if f["rule"] == "GPU_CAPACITY_CONSTRAINT"]
        if gpu:
            f = gpu[0]
            make("GPU workload cannot be scheduled", res,
                 [{"resource": res, "rule": f["rule"], "finding_id": f["id"],
                   "reason": f["summary"], "evidence": f["evidence"][:3],
                   "path": [res, "GPU capacity / scheduling constraints"], "score": 5}])

    # Collective timeout + peer fabric alerts: show the co-occurrence, not a proven cause.
    for pod in pods:
        ns, name = pod.get("namespace"), pod.get("name")
        res = _resource("Pod", ns, name)
        timeouts = [f for f in by_resource[res] if f["rule"] == "NCCL_TIMEOUT_SIGNAL"]
        if not timeouts:
            continue
        related = [p for p in pods if p.get("namespace") == ns and p.get("labels", {}).get("app") and
                   p.get("labels", {}).get("app") == pod.get("labels", {}).get("app")]
        hypotheses = []
        for peer in related:
            node = peer.get("node")
            if not node:
                continue
            node_resource = _resource("Node", "", node)
            for finding in by_resource[node_resource]:
                if finding["rule"] in ("RDMA_RETRANSMIT_SIGNAL", "PFC_CONGESTION_SIGNAL", "GPU_DEVICE_ERROR"):
                    hypotheses.append({"resource": node_resource, "rule": finding["rule"],
                                       "finding_id": finding["id"], "reason": finding["summary"],
                                       "evidence": finding["evidence"][:3],
                                       "path": [res, "distributed peer: " + _resource("Pod", ns, peer["name"]), node_resource],
                                       "score": SEVERITY.get(finding["severity"], 1) + 3})
        if not hypotheses:
            hypotheses = [{"resource": res, "rule": "NCCL_TIMEOUT_SIGNAL", "finding_id": timeouts[0]["id"],
                           "reason": "NCCL timeout observed; no matching infrastructure signal",
                           "evidence": timeouts[0]["evidence"], "path": [res], "score": 3}]
        make("Distributed training rank reports a collective timeout", res, hypotheses)
    return sorted(incidents, key=lambda x: (x["resource"], x["id"]))
