"""Explicitly requested, guarded Deployment rollout-restart. Never auto-selected."""
from datetime import datetime, timezone
from pathlib import Path
import json

ANNOTATION = "kubectl.kubernetes.io/restartedAt"


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _audit(path, record):
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"time": _stamp(), **record}, sort_keys=True) + "\n")


def restart_deployment(namespace, name, context=None, apply=False, yes=False,
                       audit_path="./kdoctor-audit.jsonl", policy=None, ticket=None):
    """Allowlisted operation, requiring a healthy replicated, unpaused deployment.

    A restart is NOT a general-purpose fix and cannot guarantee no disruption.
    No automatic rollback: another patch would trigger another rollout.
    """
    if apply and not yes:
        raise ValueError("Applying requires BOTH --apply and --yes")
    from kubernetes import client
    from .collector import connect
    _, active_context = connect(context)
    apps = client.AppsV1Api()
    dep = apps.read_namespaced_deployment(name=name, namespace=namespace, _request_timeout=20)
    spec, status = dep.spec, dep.status
    desired = spec.replicas if spec.replicas is not None else 1
    available = status.available_replicas or 0
    updated = status.updated_replicas or 0
    ready = status.ready_replicas or 0
    if getattr(spec, "paused", False) or desired < 2 or min(available, ready, updated) < desired:
        raise ValueError("Refusing restart: requires unpaused deployment with >=2 desired replicas, all available/ready/updated")
    if (status.observed_generation or 0) < (dep.metadata.generation or 0):
        raise ValueError("Refusing restart: deployment controller has not observed the current generation")
    if policy is not None:
        from .policy import authorize_restart
        authorize_restart(policy, active_context, namespace, name, ticket)
    plan = {"action": "rollout-restart", "context": active_context,
            "namespace": namespace, "deployment": name, "change_ticket": ticket,
            "desired_replicas": desired,
            "ready_replicas": ready, "effect": "Triggers rolling replacement of Pods; can disrupt service",
            "caution": "Only use after confirming a restart is the correct recovery; will not fix persistent root causes"}
    if not apply:
        return {"status": "DRY_RUN", **plan}

    # RBAC policy authorization is separate from authentication and read access.
    authz = client.AuthorizationV1Api()
    body = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
            "spec": {"resourceAttributes": {"namespace": namespace, "verb": "patch",
                                            "group": "apps", "resource": "deployments", "name": name}}}
    review = authz.create_self_subject_access_review(body=body, _request_timeout=20)
    if not review.status.allowed:
        raise PermissionError(f"Kubernetes RBAC does not allow patch deployment {namespace}/{name}")
    _audit(audit_path, {**plan, "status": "ATTEMPT"})
    annotation_value = _stamp()
    patch = {"metadata": {"resourceVersion": dep.metadata.resource_version},
             "spec": {"template": {"metadata": {"annotations": {ANNOTATION: annotation_value}}}}}
    try:
        apps.patch_namespaced_deployment(name=name, namespace=namespace, body=patch,
                                         _request_timeout=20)
        observed = apps.read_namespaced_deployment(name=name, namespace=namespace,
                                                   _request_timeout=20)
        annotations = observed.spec.template.metadata.annotations or {}
        if annotations.get(ANNOTATION) != annotation_value:
            raise RuntimeError("Patch response received but restart annotation was not verified")
    except Exception as exc:
        _audit(audit_path, {**plan, "status": "ERROR_OR_UNVERIFIED", "error": str(exc)[:300]})
        raise
    _audit(audit_path, {**plan, "status": "PATCH_VERIFIED", "annotation": annotation_value})
    return {"status": "PATCH_VERIFIED_ROLLOUT_NOT_YET_CONFIRMED", **plan,
            "annotation": annotation_value,
            "next_step": f"kubectl --context {active_context} -n {namespace} rollout status deployment/{name}"}


def set_node_schedulable(name, context=None, *, schedulable=False, apply=False, yes=False,
                         audit_path="./kdoctor-audit.jsonl", policy=None, ticket=None):
    """Explicit cordon/un-cordon with Ready-state precondition and reversible patch.

    Cordon: only a NotReady node. Uncordon: only a Ready, pressure-free node.
    No drain/delete/migration is performed; production policies may need stricter gates.
    """
    if apply and not yes:
        raise ValueError("Applying requires BOTH --apply and --yes")
    from kubernetes import client
    from .collector import connect
    _, active_context = connect(context)
    from .policy import authorize_node
    if policy is not None:
        authorize_node(policy, active_context, name, "uncordon" if schedulable else "cordon", ticket)
    core = client.CoreV1Api()
    node = core.read_node(name=name, _request_timeout=20)
    conditions = {c.type: c.status for c in node.status.conditions or []}
    currently_unschedulable = bool(node.spec.unschedulable)
    target_unschedulable = not schedulable
    if currently_unschedulable == target_unschedulable:
        raise ValueError("Refusing operation: node is already in requested scheduling state")
    if schedulable:
        if conditions.get("Ready") != "True" or any(conditions.get(x) == "True" for x in
                                                        ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")):
            raise ValueError("Refusing uncordon: node must be Ready and without reported pressure")
    else:
        if conditions.get("Ready") == "True":
            raise ValueError("Refusing cordon: node must report Ready!=True (manual outage triage only)")
    action = "uncordon-node" if schedulable else "cordon-node"
    plan = {"action": action, "context": active_context, "node": name, "change_ticket": ticket,
            "from_unschedulable": currently_unschedulable, "to_unschedulable": target_unschedulable,
            "effect": "Update scheduling eligibility only; existing pods are NOT drained or moved",
            "rollback": "Requires a separate approved uncordon on a recovered Ready node" if not schedulable
            else "Requires a separate approved cordon if node later becomes unhealthy"}
    if not apply:
        return {"status": "DRY_RUN", **plan}
    review = client.AuthorizationV1Api().create_self_subject_access_review(
        body={"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
              "spec": {"resourceAttributes": {"verb": "patch", "resource": "nodes", "name": name}}},
        _request_timeout=20)
    if not review.status.allowed:
        raise PermissionError(f"Kubernetes RBAC does not allow patch node {name}")
    _audit(audit_path, {**plan, "status": "ATTEMPT"})
    try:
        core.patch_node(name=name, body={"metadata": {"resourceVersion": node.metadata.resource_version},
                                         "spec": {"unschedulable": target_unschedulable}}, _request_timeout=20)
        observed = core.read_node(name=name, _request_timeout=20)
        if bool(observed.spec.unschedulable) != target_unschedulable:
            raise RuntimeError("Node patch accepted but scheduling-state verification failed")
    except Exception as exc:
        _audit(audit_path, {**plan, "status": "ERROR_OR_UNVERIFIED", "error": str(exc)[:300]})
        raise
    _audit(audit_path, {**plan, "status": "PATCH_VERIFIED"})
    return {"status": "PATCH_VERIFIED_NODE_STATE_ONLY", **plan,
            "next_step": "Verify scheduler/Node conditions and workload routing independently"}
