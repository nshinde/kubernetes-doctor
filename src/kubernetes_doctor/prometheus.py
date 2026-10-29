"""Explicit, opt-in Prometheus/DCGM read-only query adapter.

Do not assume GPU exporter metrics exist; failures make collection incomplete.
"""
import json
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

QUERIES = {
    "gpu_xid_errors": "max by (Hostname) (DCGM_FI_DEV_XID_ERRORS)",
    "gpu_ecc_uncorrectable": "max by (Hostname) (DCGM_FI_DEV_ECC_DBE_VOL_TOTAL)",
}


def fetch_dcgm(url, timeout=8):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Prometheus must be a valid http(s) URL without embedded credentials")
    output = []
    for metric, query in QUERIES.items():
        endpoint = url.rstrip("/") + "/api/v1/query?" + urlencode({"query": query})
        with urlopen(endpoint, timeout=timeout) as response:
            data = json.load(response)
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed for {metric}: {str(data.get('error'))[:200]}")
        for row in data.get("data", {}).get("result", [])[:2000]:
            labels = row.get("metric", {})
            try:
                value = float(row.get("value", [None, None])[1])
            except (TypeError, ValueError, IndexError):
                continue
            output.append({"source": "Prometheus/DCGM", "metric": metric, "value": value,
                           "resource": "Node/" + str(labels.get("Hostname") or labels.get("node") or "unknown"),
                           "observed_at": str(row.get("value", ["unspecified"])[0])})
    return output
