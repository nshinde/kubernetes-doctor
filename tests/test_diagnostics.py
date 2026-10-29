import json
from pathlib import Path
from kubernetes_doctor.diagnostics import diagnose
from kubernetes_doctor.topology import topology, to_mermaid
from kubernetes_doctor.cli import main

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "broken-cluster.json"


def sample():
    return json.loads(FIXTURE.read_text())


def test_diagnosis_rules():
    findings = diagnose(sample())
    rules = {f["rule"] for f in findings}
    assert {"NODE_CONDITION", "GPU_CAPACITY_CONSTRAINT", "CRASHLOOPBACKOFF", "OOM_KILLED",
            "NO_READY_ENDPOINTS", "PVC_NOT_BOUND", "DEPLOYMENT_UNAVAILABLE"} <= rules
    assert any("Insufficient nvidia.com/gpu" in " ".join(f["evidence"]) for f in findings)
    assert all(not f["automatic_fix"] for f in findings)


def test_topology_service_edges_only_ready():
    graph = topology(sample())
    assert any(e["source"] == "Deployment:web/frontend" and e["target"] == "ReplicaSet:web/frontend-6cf"
               for e in graph["edges"])
    assert any(e["source"] == "Node:worker-2" and e["target"] == "Pod:web/frontend-6cf-aaa"
               for e in graph["edges"])
    assert not any(e["relation"] == "routes_to" for e in graph["edges"])
    assert "flowchart LR" in to_mermaid(graph)


def test_ready_endpoint_creates_route():
    snap = sample()
    snap["endpoint_slices"][0]["endpoints"][0]["ready"] = True
    graph = topology(snap)
    assert {"source": "Service:web/frontend", "target": "Pod:web/frontend-6cf-aaa",
            "relation": "routes_to"} in graph["edges"]


def test_partial_scan_not_healthy():
    snap = sample()
    snap["collection_errors"] = ["nodes: 403 forbidden"]
    assert "SCAN_INCOMPLETE" in {f["rule"] for f in diagnose(snap)}


def test_cli_offline(capsys):
    code = main(["explain", "--snapshot", str(FIXTURE)])
    output = capsys.readouterr().out
    assert code == 1
    assert "GPU_CAPACITY_CONSTRAINT" in output
    assert "Possible cause" in output


def test_missing_non_selector_service_not_flagged():
    snap = sample()
    snap["services"] = [{"namespace":"web","name":"external","type":"ExternalName","selector":{}}]
    assert not any(f["rule"] == "NO_READY_ENDPOINTS" for f in diagnose(snap))


def test_fix_requires_yes():
    # This guard is evaluated before importing/accessing Kubernetes or a live cluster.
    from kubernetes_doctor.remediation import restart_deployment
    import pytest
    with pytest.raises(ValueError, match="BOTH"):
        restart_deployment("prod", "api", apply=True, yes=False)


def test_cli_snapshot(capsys):
    assert main(["snapshot", "--snapshot", str(FIXTURE)]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["context"] == "demo-gpu-cluster"
    assert "pods" in parsed
