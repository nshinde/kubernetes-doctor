"""Read-only, curated Kubernetes inventory collection (no Secrets or pod env)."""
from datetime import datetime, timezone
import os


def _get(data, *path, default=None):
    for key in path:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
    return default if data is None else data


def _owner(obj):
    return [{"kind": x.get("kind"), "name": x.get("name")}
            for x in _get(obj, "metadata", "ownerReferences", default=[])]


def _meta(obj):
    return {
        "name": _get(obj, "metadata", "name"),
        "namespace": _get(obj, "metadata", "namespace", default="default"),
    }


def _conditions(obj, path="status"):
    return [{"type": x.get("type"), "status": x.get("status"),
             "reason": x.get("reason"), "message": x.get("message")}
            for x in _get(obj, path, "conditions", default=[])]


def _project(kind, item):
    base = _meta(item)
    if kind == "nodes":
        return {"name": base["name"], "conditions": _conditions(item),
                "allocatable": _get(item, "status", "allocatable", default={}),
                "taints": _get(item, "spec", "taints", default=[]),
                "labels": _get(item, "metadata", "labels", default={}),
                "capacity": _get(item, "status", "capacity", default={}),
                "unschedulable": _get(item, "spec", "unschedulable", default=False)}
    if kind == "pods":
        containers = []
        for status in _get(item, "status", "containerStatuses", default=[]):
            containers.append({
                "name": status.get("name"), "ready": status.get("ready"),
                "restart_count": status.get("restartCount", 0),
                "waiting_reason": _get(status, "state", "waiting", "reason"),
                "waiting_message": _get(status, "state", "waiting", "message"),
                "terminated_reason": _get(status, "state", "terminated", "reason"),
                "last_terminated_reason": _get(status, "lastState", "terminated", "reason"),
            })
        gpu_requests = {}
        for container in _get(item, "spec", "containers", default=[]):
            resources = _get(container, "resources", "limits", default={})
            resources = {**resources, **_get(container, "resources", "requests", default={})}
            for resource, quantity in resources.items():
                if resource.endswith("/gpu"):
                    try:
                        gpu_requests[resource] = str(int(gpu_requests.get(resource, "0")) + int(quantity))
                    except (ValueError, TypeError):
                        gpu_requests[resource] = str(quantity)
        return {**base, "phase": _get(item, "status", "phase"),
                "node": _get(item, "spec", "nodeName"), "labels": _get(item, "metadata", "labels", default={}),
                "owners": _owner(item), "conditions": _conditions(item),
                "containers": containers, "gpu_requests": gpu_requests,
                "pvc_claims": [ _get(v, "persistentVolumeClaim", "claimName")
                               for v in _get(item, "spec", "volumes", default=[])
                               if _get(v, "persistentVolumeClaim", "claimName") ],
                "node_selector": _get(item, "spec", "nodeSelector", default={}),
                "tolerations": [{"key": t.get("key"), "effect": t.get("effect"),
                                  "operator": t.get("operator"), "value": t.get("value")}
                                 for t in _get(item, "spec", "tolerations", default=[])],
                "pod_ip": _get(item, "status", "podIP")}
    if kind == "services":
        return {**base, "type": _get(item, "spec", "type"),
                "selector": _get(item, "spec", "selector", default={}),
                "publish_not_ready_addresses": _get(item, "spec", "publishNotReadyAddresses", default=False)}
    if kind == "endpoint_slices":
        endpoints = []
        for ep in item.get("endpoints") or []:
            endpoints.append({"addresses": ep.get("addresses") or [],
                              "ready": _get(ep, "conditions", "ready", default=True),
                              "terminating": _get(ep, "conditions", "terminating", default=False),
                              "target_kind": _get(ep, "targetRef", "kind"),
                              "target_name": _get(ep, "targetRef", "name")})
        return {**base, "service": _get(item, "metadata", "labels", "kubernetes.io/service-name"),
                "endpoints": endpoints}
    if kind in ("deployments", "replica_sets"):
        return {**base, "owners": _owner(item),
                "replicas": _get(item, "spec", "replicas", default=1),
                "available_replicas": _get(item, "status", "availableReplicas", default=0)}
    if kind == "pvcs":
        return {**base, "phase": _get(item, "status", "phase"),
                "storage_class": _get(item, "spec", "storageClassName"),
                "volume_name": _get(item, "spec", "volumeName")}
    if kind == "pvs":
        return {"name": base["name"], "phase": _get(item, "status", "phase"),
                "storage_class": _get(item, "spec", "storageClassName"),
                "claim_namespace": _get(item, "spec", "claimRef", "namespace"),
                "claim_name": _get(item, "spec", "claimRef", "name")}
    if kind == "ingresses":
        services = []
        default_svc = _get(item, "spec", "defaultBackend", "service", "name")
        if default_svc:
            services.append(default_svc)
        for rule in _get(item, "spec", "rules", default=[]):
            for path in _get(rule, "http", "paths", default=[]):
                svc = _get(path, "backend", "service", "name")
                if svc and svc not in services:
                    services.append(svc)
        return {**base, "services": services}
    if kind == "network_policies":
        return {**base, "pod_selector": _get(item, "spec", "podSelector", "matchLabels", default={}),
                "policy_types": _get(item, "spec", "policyTypes", default=[])}
    if kind == "events":
        return {"namespace": base["namespace"],
                "object_kind": _get(item, "involvedObject", "kind"),
                "object_name": _get(item, "involvedObject", "name"),
                "reason": item.get("reason"), "message": item.get("message"),
                "type": item.get("type"), "count": item.get("count")}
    raise ValueError(kind)


def connect(context=None):
    """Use active kubeconfig context, or a service account when running in-cluster."""
    from kubernetes import client, config
    if os.getenv("KUBERNETES_SERVICE_HOST") and not context:
        config.load_incluster_config()
        context_name = "in-cluster"
    else:
        config.load_kube_config(context=context)
        _, context_name = config.list_kube_config_contexts()
        context_name = context or (context_name or {}).get("name", "unknown")
    return client, context_name


def collect(context=None, max_per_kind=5000):
    """Collect partial results with explicit errors; never infer health from missing RBAC data."""
    client, context_name = connect(context)
    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    discovery = client.DiscoveryV1Api()
    network = client.NetworkingV1Api()
    api_client = client.ApiClient()
    endpoints = {
        "nodes": core.list_node,
        "pods": core.list_pod_for_all_namespaces,
        "services": core.list_service_for_all_namespaces,
        "endpoint_slices": discovery.list_endpoint_slice_for_all_namespaces,
        "deployments": apps.list_deployment_for_all_namespaces,
        "replica_sets": apps.list_replica_set_for_all_namespaces,
        "pvcs": core.list_persistent_volume_claim_for_all_namespaces,
        "events": core.list_event_for_all_namespaces,
        "pvs": core.list_persistent_volume,
        "ingresses": network.list_ingress_for_all_namespaces,
        "network_policies": network.list_network_policy_for_all_namespaces,
    }
    snap = {"schema_version": 1, "context": context_name,
            "generated_at": datetime.now(timezone.utc).isoformat(), "collection_errors": []}
    for kind, method in endpoints.items():
        data = []
        token = None
        try:
            while True:
                if len(data) >= max_per_kind:
                    snap["collection_errors"].append(f"{kind}: truncated at {max_per_kind} resources")
                    break
                kwargs = {"limit": min(500, max_per_kind - len(data)), "_request_timeout": 20}
                if token:
                    kwargs["_continue"] = token
                response = method(**kwargs)
                data.extend(_project(kind, api_client.sanitize_for_serialization(obj)) for obj in response.items)
                token = getattr(response.metadata, "_continue", None)
                if not token:
                    break
                if len(data) >= max_per_kind:
                    snap["collection_errors"].append(
                        f"{kind}: truncated at {max_per_kind} resources; scan is incomplete")
                    break
        except Exception as exc:
            # Partial inventories are useful, but must never be reported as clean.
            snap["collection_errors"].append(f"{kind}: {type(exc).__name__}: {str(exc)[:240]}")
        snap[kind] = data
    return snap
