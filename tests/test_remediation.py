import sys
from types import SimpleNamespace as NS
from kubernetes_doctor import remediation


def _deployment(ready=2, annotation=None):
    return NS(
        spec=NS(replicas=2, paused=False,
                template=NS(metadata=NS(annotations={remediation.ANNOTATION: annotation} if annotation else {}))),
        status=NS(ready_replicas=ready, available_replicas=ready,
                  updated_replicas=ready, observed_generation=2),
        metadata=NS(generation=2, resource_version="10"),
    )


def test_restart_dry_run_does_not_patch(monkeypatch):
    calls = []
    apps = NS(read_namespaced_deployment=lambda **kwargs: _deployment(),
              patch_namespaced_deployment=lambda **kwargs: calls.append(kwargs))
    fake = NS(AppsV1Api=lambda: apps)
    monkeypatch.setitem(sys.modules, "kubernetes", NS(client=fake))
    monkeypatch.setattr("kubernetes_doctor.collector.connect", lambda context: (fake, "test-context"))
    plan = remediation.restart_deployment("web", "frontend")
    assert plan["status"] == "DRY_RUN"
    assert not calls


def test_restart_rejects_unavailable_deployment(monkeypatch):
    apps = NS(read_namespaced_deployment=lambda **kwargs: _deployment(ready=1))
    fake = NS(AppsV1Api=lambda: apps)
    monkeypatch.setitem(sys.modules, "kubernetes", NS(client=fake))
    monkeypatch.setattr("kubernetes_doctor.collector.connect", lambda context: (fake, "test-context"))
    import pytest
    with pytest.raises(ValueError, match="Refusing restart"):
        remediation.restart_deployment("web", "frontend")


def test_restart_apply_verifies_patch_and_writes_audit(monkeypatch, tmp_path):
    state = {"annotation": None, "count": 0}

    def read(**kwargs):
        return _deployment(annotation=state["annotation"])

    def patch(**kwargs):
        state["annotation"] = kwargs["body"]["spec"]["template"]["metadata"]["annotations"][remediation.ANNOTATION]
        state["count"] += 1

    fake = NS(
        AppsV1Api=lambda: NS(read_namespaced_deployment=read, patch_namespaced_deployment=patch),
        AuthorizationV1Api=lambda: NS(create_self_subject_access_review=lambda **kwargs: NS(status=NS(allowed=True))),
    )
    monkeypatch.setitem(sys.modules, "kubernetes", NS(client=fake))
    monkeypatch.setattr("kubernetes_doctor.collector.connect", lambda context: (fake, "test-context"))
    audit = tmp_path / "audit.jsonl"
    result = remediation.restart_deployment("web", "frontend", apply=True, yes=True, audit_path=str(audit))
    assert result["status"] == "PATCH_VERIFIED_ROLLOUT_NOT_YET_CONFIRMED"
    assert state["count"] == 1
    lines = audit.read_text().splitlines()
    assert len(lines) == 2
    assert '"status": "PATCH_VERIFIED"' in lines[1]
