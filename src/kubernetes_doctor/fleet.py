"""Multi-cluster fleet aggregation without claiming that a workload is migratable."""
from .diagnostics import diagnose
from .correlation import correlate


def gpu_capacity(snapshot):
    # Only considers already scheduled, nonterminal pods. Unschedulable pods are not counted.
    allocated = {}
    for pod in snapshot.get("pods", []):
        node = pod.get("node")
        if not node or pod.get("phase") in ("Succeeded", "Failed"):
            continue
        for kind, count in (pod.get("gpu_requests") or {}).items():
            try:
                allocated[(node, kind)] = allocated.get((node, kind), 0) + int(count)
            except (ValueError, TypeError):
                pass
    records = []
    for node in snapshot.get("nodes", []):
        for kind, total in (node.get("allocatable") or {}).items():
            if not kind.endswith("/gpu"):
                continue
            try:
                capacity = int(total)
            except (ValueError, TypeError):
                continue
            claimed = allocated.get((node["name"], kind), 0)
            ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in node.get("conditions", []))
            records.append({"node": node["name"], "resource": kind, "allocatable": capacity,
                            "requested": claimed, "unclaimed": max(0, capacity - claimed),
                            "ready": ready, "tainted": bool(node.get("taints"))})
    return sorted(records, key=lambda x: (x["node"], x["resource"]))


def analyze_fleet(snapshots):
    seen = set()
    results = []
    for snapshot in snapshots:
        context = snapshot.get("context")
        if not context or context in seen:
            raise ValueError(f"Missing or duplicate cluster context {context!r}; use unique labels")
        seen.add(context)
        findings = diagnose(snapshot)
        incidents = correlate(snapshot, findings)
        devices = gpu_capacity(snapshot)
        errors = snapshot.get("collection_errors") or []
        state = ("INCOMPLETE" if errors else "DEGRADED" if any(f["severity"] in ("critical", "high") for f in findings)
                 else "WARNING" if findings else "HEALTHY")
        results.append({"context": context, "state": state,
                        "inventory_complete": not bool(errors),
                        "node_count": len(snapshot.get("nodes", [])),
                        "pod_count": len(snapshot.get("pods", [])),
                        "findings": findings, "incidents": incidents,
                        "gpu": devices, "collection_errors": errors})
    candidates = []
    for source in snapshots:
        for pod in source.get("pods", []):
            if pod.get("node") or pod.get("phase") != "Pending" or not pod.get("gpu_requests"):
                continue
            for resource, quantity in pod["gpu_requests"].items():
                try:
                    needed = int(quantity)
                except (ValueError, TypeError):
                    continue
                for target in results:
                    if target["context"] == source["context"] or not target["inventory_complete"]:
                        continue
                    matching = [g for g in target["gpu"] if g["resource"] == resource and g["unclaimed"] >= needed
                                and g["ready"] and not g["tainted"]]
                    if matching:
                        candidates.append({"source_context": source["context"],
                                           "pending_pod": f"{pod['namespace']}/{pod['name']}",
                                           "requested": {resource: needed}, "target_context": target["context"],
                                           "candidate_nodes": [g["node"] for g in matching],
                                           "status": "CAPACITY_ONLY_UNVERIFIED",
                                           "required_checks": ["hardware/model compatibility", "nodeSelector/affinity/tolerations",
                                                               "capacity reservation", "model/weight locality and storage",
                                                               "customer/data residency", "cross-region latency and cost",
                                                               "session state and checkpoint semantics"]})
    # No production failover decision can be made with just GPU inventory.
    for cluster in results:
        cluster["capacity_note"] = ("Unclaimed GPUs are inventory estimates only. Migration eligibility requires "
                                    "model/driver compatibility, affinity, reservations, network/latency, state and tenancy checks.")
    return {"schema_version": 2, "clusters": results,
            "migration_candidates": candidates,
            "summary": {"clusters": len(results),
                        "healthy": sum(c["state"] == "HEALTHY" for c in results),
                        "incomplete": sum(not c["inventory_complete"] for c in results),
                        "findings": sum(len(c["findings"]) for c in results),
                        "incidents": sum(len(c["incidents"]) for c in results)},
            "disclaimer": "Heuristic point-in-time diagnostics; no cross-cluster failover or repair is performed."}
