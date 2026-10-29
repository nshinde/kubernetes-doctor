import copy
import io
import json
from pathlib import Path

import pytest
from kubernetes_doctor.collector import _project
from kubernetes_doctor.cli import main
from kubernetes_doctor.correlation import correlate
from kubernetes_doctor.dashboard import render_dashboard
from kubernetes_doctor.diagnostics import diagnose
from kubernetes_doctor.fleet import analyze_fleet, gpu_capacity
from kubernetes_doctor.policy import authorize_restart, propose_actions
from kubernetes_doctor.prometheus import fetch_dcgm
from kubernetes_doctor.signals import load_signals
from kubernetes_doctor.topology import topology

ROOT = Path(__file__).resolve().parents[1] / 'examples' / 'reactor-demo'


def snap(name):
    return json.loads((ROOT / name).read_text())


def test_service_fault_tree_traces_node_gpu():
    west = snap('us-west.json')
    incidents = correlate(west, diagnose(west))
    svc = next(x for x in incidents if x['resource'] == 'Service/inference/model-server')
    assert svc['primary_suspect'] == 'Node/gpu-west-a'
    assert svc['hypotheses'][0]['rule'] == 'GPU_DEVICE_ERROR'
    assert svc['hypotheses'][0]['path'][1] == 'Pod/inference/world-model-7db-a'
    assert {s['rule'] for s in svc['impact_evidence']} == {'REALTIME_LATENCY_SLO', 'SESSION_DISCONNECTS'}
    assert 'not proven' in svc['caution']


def test_gpu_request_aggregate_and_nonterminal():
    west = snap('us-west.json')
    gpus = gpu_capacity(west)
    assert next(x for x in gpus if x['node'] == 'gpu-west-a')['requested'] == 4
    assert next(x for x in gpus if x['node'] == 'gpu-west-b')['unclaimed'] == 4
    west['pods'][0]['phase'] = 'Succeeded'
    assert next(x for x in gpu_capacity(west) if x['node'] == 'gpu-west-a')['requested'] == 0


def test_multi_cluster_fleet_status_and_isolation():
    fleet = analyze_fleet([snap('us-west.json'), snap('us-east.json'), snap('eu-central.json')])
    assert fleet['summary']['clusters'] == 3
    assert fleet['summary']['incomplete'] == 0
    assert [c['state'] for c in fleet['clusters']] == ['DEGRADED', 'DEGRADED', 'HEALTHY']
    assert fleet['clusters'][2]['gpu'][0]['unclaimed'] == 6
    assert 'eligibility requires' in fleet['clusters'][2]['capacity_note']
    assert not any('migrated' in str(x) for x in fleet['clusters'])


def test_multi_cluster_duplicate_rejected():
    with pytest.raises(ValueError, match='duplicate'):
        analyze_fleet([snap('us-west.json'), snap('us-west.json')])


def test_partial_inventory_never_clean():
    eu = snap('eu-central.json')
    eu['collection_errors'] = ['nodes: missing RBAC']
    fleet = analyze_fleet([eu])
    assert fleet['clusters'][0]['state'] == 'INCOMPLETE'
    assert fleet['summary']['incomplete'] == 1


def test_gpu_ncccl_fabric_correlation():
    east = snap('us-east.json')
    incidents = correlate(east, diagnose(east))
    x = next(i for i in incidents if i['resource'] == 'Pod/training/trainer-rank-0')
    assert x['hypotheses'][0]['resource'] == 'Node/gpu-east-b'
    assert x['hypotheses'][0]['rule'] in ('RDMA_RETRANSMIT_SIGNAL', 'PFC_CONGESTION_SIGNAL')
    assert 'peer:' in x['hypotheses'][0]['path'][1]


def test_topology_ingress_pvc_node_gpu_service_selector():
    graph = topology(snap('us-west.json'))
    edges = {(e['source'], e['target'], e['relation']) for e in graph['edges']}
    assert ('Ingress:inference/world-model-api', 'Service:inference/model-server', 'forwards_to') in edges
    assert ('Service:inference/model-server', 'Pod:inference/world-model-7db-a', 'selects') in edges
    assert ('Pod:inference/world-model-7db-a', 'PVC:inference/model-cache', 'mounts') in edges
    assert ('PVC:inference/model-cache', 'PV:pvc-model-cache', 'binds_to') in edges
    assert ('Pod:inference/world-model-7db-a', 'GPUResource:gpu-west-a:nvidia.com/gpu', 'requests_gpu') in edges
    assert ('NetworkPolicy:inference/isolate-model', 'Pod:inference/world-model-7db-a', 'selects_for_policy') in edges


def test_telemetry_rule_threshold_and_source():
    west = snap('us-west.json')
    signals = [f for f in diagnose(west) if f.get('data_source') == 'external-telemetry']
    assert {'GPU_DEVICE_ERROR', 'REALTIME_LATENCY_SLO', 'SESSION_DISCONNECTS'} == {x['rule'] for x in signals}
    assert all('source=' in ' '.join(f['evidence']) for f in signals)
    west['signals'][0]['value'] = 0
    assert not any(f['rule'] == 'GPU_DEVICE_ERROR' for f in diagnose(west))


def test_signal_file_validation(tmp_path):
    valid = ROOT / 'dcgm-sample.json'
    assert load_signals([valid])[0]['metric'] == 'gpu_xid_errors'
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'signals': [{'metric': 'gpu_xid_errors', 'value': float('nan')}] }))
    with pytest.raises(ValueError, match='invalid numeric'):
        load_signals([bad])
    bad.write_text(json.dumps({'signals': [{'metric': 'other', 'value': 2}]}))
    with pytest.raises(ValueError, match='unsupported metric'):
        load_signals([bad])


def test_prometheus_dcgm_adapter(monkeypatch):
    class FakeHTTP(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    called = []
    def mock(url, timeout):
        called.append(url)
        result = {'status': 'success', 'data': {'result': [{'metric': {'Hostname': 'gpu-1'}, 'value': [1, '2']} ]}}
        return FakeHTTP(json.dumps(result).encode())
    monkeypatch.setattr('kubernetes_doctor.prometheus.urlopen', mock)
    measurements = fetch_dcgm('https://prometheus.example.net')
    assert len(measurements) == 2
    assert measurements[0]['resource'] == 'Node/gpu-1'
    assert all('/api/v1/query?' in call for call in called)
    with pytest.raises(ValueError):
        fetch_dcgm('file:///etc/passwd')


def test_repair_policy_allows_exact_target_only():
    policy = json.loads((ROOT/'restart-policy.json').read_text())
    assert authorize_restart(policy, 'reactor-demo-staging', 'sandbox', 'demo-api', 'CHG-0009')
    with pytest.raises(PermissionError, match='not allowlisted'):
        authorize_restart(policy, 'reactor-demo-staging', 'production', 'demo-api', 'CHG-0009')
    with pytest.raises(PermissionError, match='not allowlisted'):
        authorize_restart(policy, 'reactor-demo-prod', 'sandbox', 'demo-api', 'CHG-0009')
    with pytest.raises(PermissionError, match='ticket'):
        authorize_restart(policy, 'reactor-demo-staging', 'sandbox', 'demo-api', '')


def test_actions_are_non_mutating():
    west = snap('us-west.json')
    plans = propose_actions(west, diagnose(west))
    assert plans['plans']
    assert all(not p['automatic'] for p in plans['plans'])
    assert any(p['action'] == 'verify-gpu-scheduling' for p in plans['plans'])


def test_html_dashboard_escapes_untrusted_strings():
    west = snap('us-west.json')
    west['events'].append({'namespace':'inference','object_kind':'Pod','object_name':'world-model-7db-a',
                           'reason':'Warning','message':'<script>alert(1)</script>'})
    page = render_dashboard(analyze_fleet([west]), [west])
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    assert '<script>alert(1)</script>' not in page
    assert 'function filterReports()' in page
    assert 'gpu-west-a' in page


def test_cli_fleet_and_report(tmp_path, capsys):
    west, eu = str(ROOT/'us-west.json'), str(ROOT/'eu-central.json')
    assert main(['fleet','--snapshot',west,'--snapshot',eu]) == 0
    assert '2 clusters' in capsys.readouterr().out
    path = tmp_path / 'dashboard.html'
    assert main(['report','--snapshot',west,'--snapshot',eu,'-o',str(path)]) == 0
    assert '<svg' in path.read_text()


def test_cli_scan_signals_and_plan(capsys):
    eu = str(ROOT/'eu-central.json')
    signal = str(ROOT/'dcgm-sample.json')
    assert main(['scan','--snapshot',eu,'--signals',signal]) == 1
    assert 'GPU_DEVICE_ERROR' in capsys.readouterr().out
    assert main(['plan','--snapshot',str(ROOT/'us-west.json')]) == 0
    assert 'automatic' in capsys.readouterr().out


def test_cli_apply_deny_before_cluster(capsys):
    assert main(['fix','--namespace','sandbox','--deployment','demo-api','--apply','--yes']) == 3
    assert 'requires --apply' in capsys.readouterr().err


def test_projection_ingress_and_claims_excludes_env():
    ingress = {'metadata':{'name':'edge','namespace':'live'}, 'spec':{
        'rules':[{'http':{'paths':[{'backend':{'service':{'name':'svc'}}}]}}]}}
    assert _project('ingresses', ingress)['services'] == ['svc']
    pod_spec = {'metadata':{'namespace':'live','name':'p'},'spec':{'containers':[{'name':'app','env':[{'name':'TOKEN','value':'DONT_COPY'}]}],
                 'volumes':[{'name':'x','persistentVolumeClaim':{'claimName':'cache'}}]},'status':{}}
    projection = _project('pods',pod_spec)
    assert projection['pvc_claims'] == ['cache']
    assert 'DONT_COPY' not in repr(projection)


def test_diagnose_cni_dns_indicators():
    west = copy.deepcopy(snap('us-west.json'))
    west['events'] += [
        {'namespace':'inference','object_kind':'Pod','object_name':'world-model-7db-a','reason':'FailedCreatePodSandBox','message':'CNI plugin failed'},
        {'namespace':'inference','object_kind':'Pod','object_name':'world-model-7db-a','reason':'DNSConfigForming','message':'DNS nameserver limits exceeded'},
    ]
    rules = {f['rule'] for f in diagnose(west)}
    assert 'CNI_SANDBOX_FAILURE' in rules
    assert 'DNS_FAILURE_INDICATOR' in rules


def test_node_cordon_dry_run_and_reversible_apply(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace as NS
    from kubernetes_doctor.remediation import set_node_schedulable

    state = {'unschedulable': False, 'ready': 'False', 'patches': []}
    def read_node(**kwargs):
        return NS(spec=NS(unschedulable=state['unschedulable']),
                  metadata=NS(resource_version='20'),
                  status=NS(conditions=[NS(type='Ready',status=state['ready'])]))
    def patch_node(**kwargs):
        state['patches'].append(kwargs['body'])
        state['unschedulable'] = kwargs['body']['spec']['unschedulable']
    fake = NS(CoreV1Api=lambda: NS(read_node=read_node,patch_node=patch_node),
              AuthorizationV1Api=lambda: NS(create_self_subject_access_review=lambda **kw:
                                            NS(status=NS(allowed=True))))
    monkeypatch.setitem(sys.modules, 'kubernetes', NS(client=fake))
    monkeypatch.setattr('kubernetes_doctor.collector.connect', lambda context: (fake, 'reactor-demo-maintenance'))
    policy = json.loads((ROOT/'restart-policy.json').read_text())
    kw = {'context':None,'policy':policy,'ticket':'CHG-0009','audit_path':str(tmp_path/'audit.jsonl')}
    plan = set_node_schedulable('gpu-sandbox-bad', **kw)
    assert plan['status'] == 'DRY_RUN' and not state['patches']
    out = set_node_schedulable('gpu-sandbox-bad', apply=True,yes=True,**kw)
    assert out['status'] == 'PATCH_VERIFIED_NODE_STATE_ONLY'
    assert state['unschedulable'] is True
    state['ready'] = 'True'
    back = set_node_schedulable('gpu-sandbox-bad', schedulable=True, apply=True,yes=True,**kw)
    assert back['action'] == 'uncordon-node'
    assert state['unschedulable'] is False
    assert len((tmp_path/'audit.jsonl').read_text().splitlines()) == 4


def test_node_cordon_refuses_healthy_node(monkeypatch):
    import sys
    from types import SimpleNamespace as NS
    from kubernetes_doctor.remediation import set_node_schedulable
    fake = NS(CoreV1Api=lambda: NS(read_node=lambda **kw: NS(
        spec=NS(unschedulable=False),status=NS(conditions=[NS(type='Ready',status='True')]))))
    monkeypatch.setitem(sys.modules, 'kubernetes', NS(client=fake))
    monkeypatch.setattr('kubernetes_doctor.collector.connect', lambda context: (fake, 'test'))
    with pytest.raises(ValueError,match='Refusing cordon'):
        set_node_schedulable('healthy-node')


def test_cli_node_requires_name(capsys):
    assert main(['fix','--action','cordon-node']) == 3
    assert 'requires --node' in capsys.readouterr().err


def test_cross_region_capacity_candidate_is_advisory():
    fleet = analyze_fleet([snap('us-west.json'),snap('eu-central.json')])
    candidates = fleet['migration_candidates']
    assert any(x['target_context'] == 'reactor-demo-eu-central' for x in candidates)
    assert all(x['status'] == 'CAPACITY_ONLY_UNVERIFIED' for x in candidates)
    assert all('session state and checkpoint semantics' in x['required_checks'] for x in candidates)
