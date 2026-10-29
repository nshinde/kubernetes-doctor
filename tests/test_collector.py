from kubernetes_doctor.collector import _project


def test_pod_projection_is_minimal_and_sums_gpus():
    original = {
        "metadata": {"namespace": "training", "name": "pod", "annotations": {"private": "do-not-copy"}},
        "spec": {"containers": [
            {"name": "main", "env": [{"name": "SECRET", "value": "do-not-copy"}],
             "resources": {"requests": {"nvidia.com/gpu": "2"}}},
            {"name": "sidecar", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]},
        "status": {"phase": "Pending", "containerStatuses": []},
    }
    result = _project("pods", original)
    assert result["gpu_requests"] == {"nvidia.com/gpu": "3"}
    assert "do-not-copy" not in str(result)
    assert "env" not in str(result)


def test_endpoint_slice_projection_preserves_true_for_null_ready():
    obj = {"metadata": {"name": "foo-a", "namespace": "web",
                        "labels": {"kubernetes.io/service-name": "foo"}},
           "endpoints": [{"addresses": ["10.0.0.1"], "conditions": {"ready": None}}]}
    assert _project("endpoint_slices", obj)["endpoints"][0]["ready"] is True
