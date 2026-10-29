"""Deterministic evidence-backed rules, no cluster mutation."""
from collections import defaultdict
from .signals import diagnose_signals

PRIORITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def diagnose(snapshot):
    findings = []
    event_index = defaultdict(list)
    for event in snapshot.get("events", []):
        key = (event.get("namespace"), event.get("object_kind"), event.get("object_name"))
        if event.get("message"):
            event_index[key].append(f"{event.get('reason')}: {event['message'][:350]}")

    def add(rule, severity, kind, ns, name, summary, evidence, cause, action):
        events = event_index.get((ns, kind, name), [])[-3:]
        resource = f"{kind}/{ns + '/' if ns else ''}{name}"
        findings.append({"id": f"{rule}:{resource}", "rule": rule, "severity": severity,
                         "resource": resource, "summary": summary,
                         "evidence": evidence + (["Recent events: " + " | ".join(events)] if events else []),
                         "possible_cause": cause, "recommendation": action,
                         "automatic_fix": False})

    for node in snapshot.get("nodes", []):
        for condition in node.get("conditions", []):
            typ, value = condition.get("type"), condition.get("status")
            bad = (typ == "Ready" and value != "True") or (
                typ in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable") and value == "True")
            if bad:
                add("NODE_CONDITION", "high", "Node", "", node["name"],
                    f"Node {typ}={value}", [f"Condition: {typ}={value}", condition.get("message") or ""],
                    "Node health, resource pressure or network configuration",
                    "Inspect kubelet/node events, node resources and CNI before considering drain/reboot")

    for pod in snapshot.get("pods", []):
        ns, name = pod["namespace"], pod["name"]
        scheduled = next((c for c in pod.get("conditions", []) if c.get("type") == "PodScheduled"), None)
        unschedulable = (scheduled or {}).get("reason") == "Unschedulable"
        if unschedulable:
            msg = scheduled.get("message") or "Scheduler could not place pod"
            gpu = pod.get("gpu_requests") or {}
            gpu_insufficient = gpu and any(f"Insufficient {r}" in msg for r in gpu)
            rule = "GPU_CAPACITY_CONSTRAINT" if gpu_insufficient else "UNSCHEDULABLE"
            add(rule, "high", "Pod", ns, name, "Pod cannot be scheduled",
                [msg, f"GPU requests: {gpu or 'none'}"],
                ("Scheduler reports insufficient GPU capacity on eligible nodes; taints/affinity can also limit eligible nodes"
                 if gpu_insufficient else "Resource capacity, taint/toleration, affinity, or volume scheduling constraint"),
                "Check per-node GPU allocatable/claims, NVIDIA/AMD device plugin, taints, affinity and PVCs"
                if gpu_insufficient else
                "Check node allocatable vs existing allocations, taints/tolerations, affinity and PVCs")
        elif pod.get("phase") == "Pending":
            add("POD_PENDING", "medium", "Pod", ns, name, "Pod is Pending",
                ["Pod phase=Pending"], "Image initialization, volumes or scheduling in progress",
                "Inspect pod events and scheduling conditions; avoid blindly deleting the pod")

        for event in snapshot.get("events", []):
            if event.get("namespace") == ns and event.get("object_kind") == "Pod" and event.get("object_name") == name:
                reason = event.get("reason") or ""
                msg = event.get("message") or ""
                if reason in ("FailedCreatePodSandBox", "FailedMount"):
                    rule = "CNI_SANDBOX_FAILURE" if reason == "FailedCreatePodSandBox" else "VOLUME_MOUNT_FAILURE"
                    add(rule, "high", "Pod", ns, name, f"Pod event: {reason}", [msg[:350]],
                        "CNI setup issue or network plugin" if reason == "FailedCreatePodSandBox" else "CSI/storage or mount issue",
                        "Inspect CNI plugin and node runtime logs" if reason == "FailedCreatePodSandBox" else "Inspect CSI driver and PVC events")
                elif (reason in ("Unhealthy", "DNSConfigForming") and
                      any(term in msg.lower() for term in ("dns", "lookup", "resolv", "nameserver"))):
                    add("DNS_FAILURE_INDICATOR", "medium", "Pod", ns, name, "Pod has DNS-related event",
                        [f"reason={reason}", msg[:350]], "DNS configuration or DNS resolution issue (tentative)",
                        "Check CoreDNS readiness, resolv.conf, network policy and DNS query logs")

        for status in pod.get("containers", []):
            reason = status.get("waiting_reason")
            container_name = status.get("name") or "unknown"
            if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"):
                cause = ("Image registry, credentials, image name or networking" if "Image" in reason else
                         "Application startup failure or invalid configuration")
                add(reason.upper(), "high", "Pod", ns, name,
                    f"Container {container_name}: {reason}",
                    [f"Waiting reason={reason}", status.get("waiting_message") or ""], cause,
                    "Inspect container state, previous logs, image and relevant events; fix the underlying cause")
            if status.get("last_terminated_reason") == "OOMKilled" or status.get("terminated_reason") == "OOMKilled":
                add("OOM_KILLED", "high", "Pod", ns, name,
                    f"Container {container_name} was OOMKilled",
                    [f"restartCount={status.get('restart_count')}", "Termination reason=OOMKilled"],
                    "Container exceeded memory limit or experienced severe memory pressure",
                    "Compare memory usage, requests/limits and application allocations; do not simply restart")

    slices = defaultdict(list)
    for eps in snapshot.get("endpoint_slices", []):
        if eps.get("service"):
            slices[(eps["namespace"], eps["service"])].extend(eps.get("endpoints", []))
    for svc in snapshot.get("services", []):
        if svc.get("type") == "ExternalName" or not svc.get("selector"):
            continue
        key = (svc["namespace"], svc["name"])
        endpoints = slices[key]
        ready = [ep for ep in endpoints if ep.get("ready") is not False and ep.get("terminating") is not True]
        if not ready and not any("endpoint_slices:" in error for error in snapshot.get("collection_errors", [])):
            add("NO_READY_ENDPOINTS", "high", "Service", *key,
                "Service has no ready EndpointSlice backends",
                [f"EndpointSlice endpoints={len(endpoints)}, ready={len(ready)}"],
                "Service selector mismatch, failing readiness probes, or no matching ready pods",
                "Compare Service selectors with pod labels; inspect pod readiness and EndpointSlices")

    for ingress in snapshot.get("ingresses", []):
        ns = ingress.get("namespace")
        unhealthy = [svc.get("name") for svc in snapshot.get("services", [])
                     if svc.get("namespace") == ns and svc.get("name") in ingress.get("services", [])
                     and any(f["rule"] == "NO_READY_ENDPOINTS" and f["resource"] == f"Service/{ns}/{svc['name']}"
                             for f in findings)]
        if unhealthy:
            add("INGRESS_BACKEND_UNAVAILABLE", "high", "Ingress", ns, ingress["name"],
                "Ingress references Services without ready endpoints", [f"services={','.join(unhealthy)}"],
                "Downstream Service or pod readiness failure", "Inspect Service/Pod dependency chain")

    for node in snapshot.get("nodes", []):
        if node.get("unschedulable"):
            add("NODE_CORDONED", "low", "Node", "", node["name"], "Node is cordoned",
                ["spec.unschedulable=true"], "Operator or automation disabled scheduling",
                "Verify intentional maintenance before enabling scheduling")

    for pvc in snapshot.get("pvcs", []):
        if pvc.get("phase") in ("Pending", "Lost"):
            add("PVC_NOT_BOUND", "high", "PersistentVolumeClaim", pvc["namespace"], pvc["name"],
                f"PVC phase={pvc['phase']}", [f"storageClass={pvc.get('storage_class') or 'default/none'}"],
                "Storage provisioning or volume binding failure",
                "Inspect PVC events, StorageClass, CSI controller and available volumes")

    for dep in snapshot.get("deployments", []):
        if (dep.get("available_replicas") or 0) < (dep.get("replicas") or 0):
            add("DEPLOYMENT_UNAVAILABLE", "medium", "Deployment", dep["namespace"], dep["name"],
                "Deployment has fewer available replicas than desired",
                [f"available={dep.get('available_replicas')}, desired={dep.get('replicas')}"],
                "Underlying pod failure, scheduling or readiness problem",
                "Trace Deployment → ReplicaSet → Pod, then inspect pod findings")

    if snapshot.get("collection_errors"):
        add("SCAN_INCOMPLETE", "high", "Cluster", "", snapshot.get("context", "unknown"),
            "Inventory incomplete: some checks may be missing",
            snapshot["collection_errors"], "Insufficient RBAC permissions, API failure or collection cap",
            "Grant only required read RBAC, resolve API errors and rerun; do not infer cluster health")
    findings.extend(diagnose_signals(snapshot))
    return sorted(findings, key=lambda f: (PRIORITY[f["severity"]], f["rule"], f["resource"]))
