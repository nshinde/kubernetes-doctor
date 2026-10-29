"""Kubernetes resource dependency graph. Edges to Pods reflect ready EndpointSlices."""
import json


def topology(snapshot):
    nodes, edges = {}, []
    seen = set()

    def put(kind, namespace, name):
        if not name:
            return None
        key = f"{kind}:{namespace + '/' if namespace else ''}{name}"
        nodes[key] = {"id": key, "kind": kind, "name": name, "namespace": namespace}
        return key

    def link(a, b, relation):
        if a and b and (a, b, relation) not in seen:
            seen.add((a, b, relation))
            edges.append({"source": a, "target": b, "relation": relation})

    for node in snapshot.get("nodes", []):
        put("Node", "", node["name"])
    for kind, key in (("Deployment", "deployments"), ("ReplicaSet", "replica_sets"),
                      ("Pod", "pods"), ("Service", "services"),
                      ("PVC", "pvcs"), ("Ingress", "ingresses"), ("NetworkPolicy", "network_policies")):
        for obj in snapshot.get(key, []):
            ns, name = obj["namespace"], obj["name"]
            namespace = put("Namespace", "", ns)
            target = put(kind, ns, name)
            link(namespace, target, "contains")
            if kind == "Pod":
                link(put("Node", "", obj.get("node")), target, "schedules")
                for claim in obj.get("pvc_claims", []):
                    link(target, put("PVC", ns, claim), "mounts")
                for resource, quantity in obj.get("gpu_requests", {}).items():
                    if quantity and obj.get("node"):
                        link(target, put("GPUResource", "", obj["node"] + ":" + resource), "requests_gpu")
            if kind == "Ingress":
                for svc in obj.get("services", []):
                    link(target, put("Service", ns, svc), "forwards_to")
            if kind == "PVC" and obj.get("volume_name"):
                link(target, put("PV", "", obj["volume_name"]), "binds_to")
            if kind == "NetworkPolicy":
                selector = obj.get("pod_selector") or {}
                for pod in snapshot.get("pods", []):
                    if pod.get("namespace") == ns and all(pod.get("labels", {}).get(k) == v for k, v in selector.items()):
                        link(target, put("Pod", ns, pod["name"]), "selects_for_policy")
            for owner in obj.get("owners", []):
                if owner.get("kind") in ("Deployment", "ReplicaSet", "DaemonSet", "StatefulSet", "Job"):
                    link(put(owner["kind"], ns, owner.get("name")), target, "owns")
    for pv in snapshot.get("pvs", []):
        put("PV", "", pv["name"])
    for svc in snapshot.get("services", []):
        ns, name = svc.get("namespace"), svc.get("name")
        for pod in snapshot.get("pods", []):
            selector = svc.get("selector") or {}
            if selector and pod.get("namespace") == ns and all(pod.get("labels", {}).get(k) == v for k, v in selector.items()):
                link(put("Service", ns, name), put("Pod", ns, pod["name"]), "selects")
    for eps in snapshot.get("endpoint_slices", []):
        ns, svc = eps["namespace"], eps.get("service")
        source = put("Service", ns, svc) if svc else None
        for ep in eps.get("endpoints", []):
            if ep.get("ready") is False or ep.get("terminating") is True:
                continue
            if ep.get("target_kind") == "Pod" and ep.get("target_name"):
                target = put("Pod", ns, ep["target_name"])
            else:
                addresses = ep.get("addresses") or []
                target = put("Endpoint", ns, addresses[0]) if addresses else None
            link(source, target, "routes_to")
    return {"context": snapshot.get("context"), "nodes": sorted(nodes.values(), key=lambda x: x["id"]),
            "edges": sorted(edges, key=lambda e: (e["source"], e["target"], e["relation"])),
            "collection_errors": snapshot.get("collection_errors", [])}


def to_mermaid(graph):
    # Generate stable opaque IDs instead of allowing K8s names to become Mermaid syntax.
    ids = {node["id"]: f"n{i}" for i, node in enumerate(graph["nodes"])}
    lines = ["flowchart LR"]
    for node in graph["nodes"]:
        label = f"{node['kind']}\\n{node['name']}".replace('"', "'").replace("<", "(").replace(">", ")")
        lines.append(f'  {ids[node["id"]]}["{label}"]')
    for edge in graph["edges"]:
        lines.append(f'  {ids[edge["source"]]} -->|{edge["relation"]}| {ids[edge["target"]]}')
    return "\n".join(lines) + "\n"
