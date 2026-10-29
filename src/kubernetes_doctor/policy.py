"""Deny-by-default deployment restart policy, and offline evidence-based suggestions."""
import json
from pathlib import Path


def load_policy(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Remediation policy must be JSON object")
    return document


def authorize_restart(policy, context, namespace, deployment, ticket):
    if not policy or policy.get("version") != 1:
        raise PermissionError("Denied: explicit version 1 remediation policy is required")
    if context not in policy.get("allowed_contexts", []):
        raise PermissionError(f"Denied: cluster context {context!r} is not allowlisted")
    if not isinstance(ticket, str) or len(ticket.strip()) < 5:
        raise PermissionError("Denied: a change ticket (at least five characters) is required")
    target = f"{namespace}/{deployment}"
    if target not in policy.get("restart_deployments", []):
        raise PermissionError(f"Denied: Deployment {target!r} not allowlisted")
    return True


def propose_actions(snapshot, findings):
    """Suggestions only. Never directly execute or infer restart fixes a failing workload."""
    templates = {
        "GPU_CAPACITY_CONSTRAINT": ("verify-gpu-scheduling", "Check eligible GPUs, node selectors, taints, tolerations and reservations"),
        "NO_READY_ENDPOINTS": ("inspect-service-backends", "Inspect selector, EndpointSlices, readiness and matching Pod errors"),
        "NODE_CONDITION": ("inspect-node", "Check kubelet health, pressure, runtime, CNI and hardware"),
        "CRASHLOOPBACKOFF": ("inspect-previous-logs", "Inspect container previous logs and startup configuration"),
        "NCCL_TIMEOUT_SIGNAL": ("investigate-fabric", "Compare NCCL rank traces, RDMA retries and switch counters"),
        "GPU_DEVICE_ERROR": ("escalate-gpu-health", "Inspect DCGM and Xid logs; consider isolation with approval"),
        "REALTIME_LATENCY_SLO": ("investigate-latency", "Correlate GPU stalls, network and session metrics"),
        "CNI_SANDBOX_FAILURE": ("investigate-cni", "Inspect failed sandbox events and CNI daemon logs"),
        "PVC_NOT_BOUND": ("investigate-storage", "Inspect PVC, StorageClass and CSI controller"),
    }
    out = []
    for finding in findings:
        template = templates.get(finding["rule"])
        if template:
            out.append({"finding_id": finding["id"], "resource": finding["resource"],
                        "action": template[0], "reason": template[1],
                        "execution": "human-run investigation; no Kubernetes mutation", "automatic": False})
    return {"context": snapshot.get("context"), "plans": out,
            "mutation_policy": "Deny by default. Only allowlisted healthy Deployment restarts supported by fix --apply."}


def authorize_node(policy, context, node, action, ticket):
    if not policy or policy.get("version") != 1:
        raise PermissionError("Denied: explicit version 1 remediation policy is required")
    if context not in policy.get("allowed_contexts", []):
        raise PermissionError(f"Denied: cluster context {context!r} not allowlisted")
    if not isinstance(ticket, str) or len(ticket.strip()) < 5:
        raise PermissionError("Denied: change ticket required")
    if action not in ("cordon", "uncordon") or node not in policy.get(action + "_nodes", []):
        raise PermissionError(f"Denied: {action} of node {node!r} not allowlisted")
    return True
