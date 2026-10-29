"""Optional, source-labeled telemetry inputs. Never scrape credentials or infer a sensor exists.

Input contract: {"signals": [{"source": "dcgm", "metric": "gpu_xid_errors",
  "value": 1, "resource": "Node/gpu-a", "observed_at": "..."}]}
Compatible exporters must explicitly map their native data into this contract.
"""
import json
import math
from pathlib import Path

# Metric names/thresholds are demo defaults; validate thresholds for your SLOs.
METRICS = {
    "gpu_xid_errors": ("GPU_DEVICE_ERROR", "critical", "gt", 0, "Check GPU Xid logs, device plugin and DCGM; inspect node before rescheduling"),
    "gpu_ecc_uncorrectable": ("GPU_ECC_ERROR", "critical", "gt", 0, "Inspect GPU health and node isolation; consult hardware operator"),
    "nccl_timeout_count": ("NCCL_TIMEOUT_SIGNAL", "critical", "gt", 0, "Correlate rank logs, collectives, network/RDMA counters and GPU health"),
    "rdma_retransmit_rate": ("RDMA_RETRANSMIT_SIGNAL", "high", "gt", 0.01, "Inspect RDMA path, congestion, packet loss, ECN and fabric counters"),
    "pfc_pause_ms_5m": ("PFC_CONGESTION_SIGNAL", "high", "gt", 1000, "Correlate PFC pauses with NIC, switch and workload stall times"),
    "network_drops_5m": ("NETWORK_DROPS_SIGNAL", "high", "gt", 100, "Inspect host interface, CNI, network policies and upstream links"),
    "frame_p99_ms": ("REALTIME_LATENCY_SLO", "high", "gt", 50, "Correlate frame latency with GPU, model and network metrics"),
    "session_disconnects_5m": ("SESSION_DISCONNECTS", "high", "gt", 0, "Inspect session lifecycle, backend health, LB resets and checkpoints"),
    "gpu_memory_used_pct": ("GPU_MEMORY_PRESSURE_SIGNAL", "medium", "gt", 95, "Inspect model weights, KV cache and per-device allocation; threshold is advisory"),
}


def load_signals(paths):
    readings = []
    for path in paths:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("signals"), list):
            raise ValueError(f"{path}: expected JSON object with signals array")
        for point in doc["signals"]:
            if not isinstance(point, dict) or not isinstance(point.get("metric"), str):
                raise ValueError(f"{path}: malformed signal")
            metric = point["metric"]
            if metric not in METRICS:
                raise ValueError(f"{path}: unsupported metric {metric!r}; supported: {', '.join(METRICS)}")
            value = point.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{path}: invalid numeric value for {metric}")
            readings.append({"metric": metric, "value": value,
                             "source": str(point.get("source") or "unattributed")[:80],
                             "resource": str(point.get("resource") or "Cluster")[:180],
                             "observed_at": str(point.get("observed_at") or "unspecified")[:80]})
    return readings


def diagnose_signals(snapshot):
    findings = []
    for i, point in enumerate(snapshot.get("signals", [])):
        metric = point.get("metric")
        if metric not in METRICS:
            continue
        rule, severity, op, threshold, advice = METRICS[metric]
        value = point.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            continue
        if not (value > threshold):
            continue
        resource = point.get("resource") or "Cluster"
        findings.append({
            "id": f"{rule}:{resource}:{i}", "rule": rule, "severity": severity,
            "resource": resource, "summary": f"Telemetry {metric}={value:g} exceeds advisory threshold {threshold:g}",
            "evidence": [f"source={point.get('source', 'unattributed')}",
                         f"observed_at={point.get('observed_at', 'unspecified')}",
                         f"{metric}={value:g}"],
            "possible_cause": "Abnormal telemetry signal; not sufficient by itself to prove root cause",
            "recommendation": advice, "automatic_fix": False,
            "confidence": "observed-signal", "data_source": "external-telemetry",
        })
    return findings
