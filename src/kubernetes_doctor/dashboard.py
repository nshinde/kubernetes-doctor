"""Single-file HTML diagnostic dashboard (offline; no CDNs or external scripts)."""
import html
import json
from .topology import topology


def _h(value):
    return html.escape(str(value), quote=True)


def _card(title, value, note=""):
    return f'<article class="stat"><span>{_h(title)}</span><strong>{_h(value)}</strong><small>{_h(note)}</small></article>'


def _graph(snapshot):
    graph = topology(snapshot)
    # A bounded local SVG is a topology *overview*, not a claim of physical network links.
    items = [x for x in graph["nodes"] if x["kind"] not in ("Namespace", "GPUResource", "PV")][:35]
    order = {"Ingress": 0, "Service": 1, "Deployment": 2, "ReplicaSet": 3,
             "Pod": 4, "PVC": 4, "Node": 5, "NetworkPolicy": 3}
    items.sort(key=lambda x: (order.get(x["kind"], 6), x["name"]))
    positions = {}
    by_level = {}
    for node in items:
        by_level.setdefault(order.get(node["kind"], 6), []).append(node)
    y_unit = 69
    for col, group in by_level.items():
        for n, item in enumerate(group):
            positions[item["id"]] = (22 + col * 205, 20 + n * y_unit)
    height = max(200, 48 + max((len(group) for group in by_level.values()), default=0) * y_unit)
    width = 1370
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Resource dependency graph">',
             '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8" fill="#6a8dac"/></marker></defs>']
    for edge in graph["edges"]:
        src, dst = positions.get(edge["source"]), positions.get(edge["target"])
        if src is None or dst is None:
            continue
        if edge["relation"] not in ("forwards_to", "routes_to", "selects", "owns", "schedules", "mounts"):
            continue
        a = (src[0] + 173, src[1] + 17)
        b = (dst[0], dst[1] + 17)
        if a[0] > b[0]:
            a, b = (src[0] + 85, src[1] + 34), (dst[0] + 85, dst[1])
        parts.append(f'<path d="M{a[0]} {a[1]} L{b[0]} {b[1]}" stroke="#507394" stroke-width="1.3" opacity=".48" fill="none" marker-end="url(#arrow)"><title>{_h(edge["relation"])}</title></path>')
    for item in items:
        x, y = positions[item["id"]]
        parts.append(f'<g><rect x="{x}" y="{y}" width="174" height="39" rx="7" fill="#132b48" stroke="#45617d"/>'
                     f'<text x="{x+9}" y="{y+14}" font-size="10" fill="#66d3ec">{_h(item["kind"])}</text>'
                     f'<text x="{x+9}" y="{y+29}" font-size="12" fill="white">{_h(item["name"][:22])}</text>'
                     f'<title>{_h(item["id"])}</title></g>')
    if len(graph["nodes"]) > len(items):
        parts.append(f'<text x="10" y="{height-8}" font-size="12" fill="#adc6d8">Showing {len(items)} of {len(graph["nodes"])} resources</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_dashboard(fleet, snapshots):
    cards, report_parts, graphs = [], [], []
    states = {x["context"]: x for x in fleet["clusters"]}
    for snapshot in snapshots:
        context = snapshot["context"]
        cluster = states[context]
        if cluster["state"] == "INCOMPLETE":
            status = "partial inventory"
        else:
            status = cluster["state"].lower()
        cards.append(_card(context, status, f'{cluster["node_count"]} nodes / {cluster["pod_count"]} pods'))
        graphs.append(f'<section><h3>{_h(context)} · dependency topology</h3><div class="graph">{_graph(snapshot)}</div></section>')
        incidents = cluster["incidents"]
        report_parts.append(f'<section><h2>{_h(context)} <span class="tag">{_h(cluster["state"])}</span></h2>')
        for incident in incidents:
            suspect = incident["hypotheses"][0]
            path = " → ".join(suspect["path"])
            report_parts.append(f'<article class="incident" data-severity="{_h(incident["severity"])}">'
                                f'<div class="eyebrow">IMPACT · {_h(incident["severity"].upper())}</div>'
                                f'<h3>{_h(incident["resource"])}</h3><p>{_h(incident["impact"])}</p>'
                                f'<p class="chain">{_h(path)}</p><p><b>Top hypothesis:</b> {_h(suspect["rule"])} — {_h(suspect["reason"])}</p>'
                                f'<details><summary>Evidence and alternative explanations</summary>')
            for hyp in incident["hypotheses"]:
                report_parts.append(f'<p><b>{_h(hyp["resource"])}</b> / {_h(hyp["rule"])}: {_h(hyp["reason"])}'
                                    f'<br><small>{_h(" | ".join(hyp["evidence"]))}</small></p>')
            report_parts.append('</details><small>Hypotheses require verification; no automated fix was applied.</small></article>')
        for finding in cluster["findings"]:
            report_parts.append(f'<details class="finding" data-severity="{_h(finding["severity"])}">'
                                f'<summary><span class="sev {_h(finding["severity"])}">{_h(finding["severity"].upper())}</span>'
                                f' {_h(finding["rule"])} <em>{_h(finding["resource"])}</em></summary>'
                                f'<p>{_h(finding["summary"])}</p><p><b>Evidence:</b> {_h("; ".join(finding["evidence"]))}</p>'
                                f'<p><b>Next:</b> {_h(finding["recommendation"])}</p></details>')
        if not incidents and not cluster["findings"]:
            report_parts.append('<p>No findings from available inventory and signals. This is not a guarantee of health.</p>')
        report_parts.append('<h3>GPU scheduling inventory</h3><table><tr><th>Node</th><th>GPU resource</th><th>Allocatable</th><th>Requested</th><th>Unclaimed</th><th>Ready</th></tr>')
        for gpu in cluster["gpu"]:
            report_parts.append('<tr>' + ''.join(f'<td>{_h(gpu[key])}</td>' for key in
                                                      ("node", "resource", "allocatable", "requested", "unclaimed", "ready")) + '</tr>')
        report_parts.append('</table><small>{}</small></section>'.format(_h(cluster["capacity_note"])))
    candidates = []
    for candidate in fleet.get("migration_candidates", []):
        candidates.append(f'<tr><td>{_h(candidate["pending_pod"])}</td>'
                          f'<td>{_h(candidate["source_context"])}</td><td>{_h(candidate["target_context"])}</td>'
                          f'<td>{_h(", ".join(candidate["candidate_nodes"]))}</td>'
                          f'<td>{_h(candidate["status"])}</td></tr>')
    candidate_html = ('<section><h2>Potential cross-cluster placement · capacity screen only</h2>'
                      '<p>These nodes are not approved failover targets. Session relocation requires a separate '
                      'compatibility, reservation, latency, tenancy and state validation.</p>'
                      '<table><tr><th>Pending pod</th><th>Current cluster</th><th>Possible cluster</th>'
                      '<th>Candidate nodes</th><th>Status</th></tr>' + ''.join(candidates) + '</table></section>')
    counts = fleet["summary"]
    style = '''
:root{color-scheme:dark;background:#071321;color:#ecf3fa;font-family:Inter,system-ui,Arial,sans-serif}
*{box-sizing:border-box}body{margin:0;line-height:1.5}main{max-width:1360px;margin:auto;padding:32px}
header{background:linear-gradient(110deg,#092945,#0d1c31 65%,#102e3e);padding:40px;border:1px solid #315071;border-radius:20px}
h1{font-size:40px;letter-spacing:-1.5px;margin:6px 0}h2{font-size:24px}h3{font-size:17px;margin:8px 0}.eyebrow{color:#52cce2;font-size:12px;letter-spacing:2px;font-weight:700}
small{color:#9eb5cc}p{color:#c8d6e4}.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:24px 0}
.stat{display:flex;flex-direction:column;background:#11243c;padding:22px;border:1px solid #314661;border-radius:14px}.stat span{color:#9eb5cc;font-size:13px}.stat strong{font-size:25px}.stat small{margin-top:6px}
section{margin:32px 0}.incident{background:#122943;border:1px solid #3c617d;border-left:4px solid #ff7f7c;padding:20px;margin:14px 0;border-radius:9px}
.incident .chain{background:#0b1a2d;padding:11px 14px;border-radius:8px;color:#79e4d2;overflow-wrap:anywhere}details{margin:10px 0;padding:13px;background:#0f2136;border:1px solid #2a4964;border-radius:9px}
summary{cursor:pointer;font-weight:600}.finding em{float:right;color:#9eb5cc;font-style:normal;font-size:12px}.sev{margin-right:10px;font-size:11px;padding:4px 6px;border-radius:4px}.high,.critical{background:#7c2635;color:#fff}.medium{background:#85632e;color:#fff}.low{background:#34546f;color:#fff}
.tag{font-size:12px;color:#86d4e0;border:1px solid #3d8294;padding:4px 7px;border-radius:6px}.graph{background:#0c2138;overflow:auto;border-radius:12px;padding:8px;border:1px solid #33526a}.graph svg{width:100%;min-width:960px;max-height:650px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;border-bottom:1px solid #2b4560;padding:10px}th{color:#8cbace}input,select{background:#122943;color:#fff;border:1px solid #4a637d;border-radius:6px;padding:10px;margin-right:8px}
footer{margin-top:50px;padding:20px;border-top:1px solid #29435d;color:#94aabd}
'''
    script = '''
function filterReports(){const q=document.getElementById('search').value.toLowerCase();const level=document.getElementById('severity').value;
 document.querySelectorAll('.finding,.incident').forEach(x=>{x.style.display=(x.textContent.toLowerCase().includes(q)&&(level==='all'||x.dataset.severity===level))?'':'none';});}
'''
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Kubernetes Doctor — Fleet Diagnosis</title><style>{style}</style></head><body><main>'
            f'<header><div class="eyebrow">CLUSTER INTELLIGENCE / OFFLINE DEMO</div><h1>◈ Kubernetes Doctor</h1>'
            f'<p>Topology → evidence → ranked hypotheses → guarded operator action</p>'
            f'<p><small>Snapshots and optional telemetry are point-in-time; no actual remediation, relocation or session recovery is implied.</small></p></header>'
            f'<div class="stat-grid">{_card("Clusters",counts["clusters"])}{_card("Findings",counts["findings"])}'
            f'{_card("Correlated incidents",counts["incidents"])}{_card("Incomplete inventories",counts["incomplete"])}</div>'
            f'<div class="stat-grid">{"".join(cards)}</div><h2>Cross-resource incident analysis</h2>'
            f'<label for="search">Filter by resource, rule, or symptom: </label><input id="search" oninput="filterReports()" placeholder="GPU, Service, Xid, RDMA ...">'
            f'<select id="severity" onchange="filterReports()"><option value="all">All severities</option><option>critical</option><option>high</option><option>medium</option><option>low</option></select>'
            f'{"".join(report_parts)}{candidate_html}<h2>Discovered topology</h2>{"".join(graphs)}'
            f'<footer>Demo-grade evidence correlation. Static report with no network connections, external dependencies, or cluster mutations.</footer>'
            f'<script>{script}</script></main></body></html>')
