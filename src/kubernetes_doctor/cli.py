"""kdoctor CLI. Live inspections are read-only; mutations require explicit policy gates."""
import argparse
import json
import sys
from pathlib import Path
from .diagnostics import diagnose
from .topology import topology, to_mermaid
from .correlation import correlate
from .fleet import analyze_fleet
from .policy import propose_actions


def _load_snapshot(path, signals=None, prometheus=None):
    snap = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(snap, dict):
        raise ValueError(f"{path}: snapshot must be a JSON object")
    return _enrich(snap, signals, prometheus)


def _enrich(snap, signals=None, prometheus=None):
    if signals:
        from .signals import load_signals
        snap.setdefault("signals", []).extend(load_signals(signals))
    if prometheus:
        from .prometheus import fetch_dcgm
        try:
            snap.setdefault("signals", []).extend(fetch_dcgm(prometheus))
        except Exception as exc:
            snap.setdefault("collection_errors", []).append(f"prometheus: {type(exc).__name__}: {str(exc)[:220]}")
    return snap


def _read_snapshot(args):
    if args.snapshot:
        return _load_snapshot(args.snapshot, args.signals, args.prometheus)
    from .collector import collect
    return _enrich(collect(context=args.context), args.signals, args.prometheus)


def _fleet_snapshots(args):
    if args.prometheus and (len(args.snapshot or []) + len(args.context or []) > 1 or args.all_contexts):
        raise ValueError("Prometheus query source must be scoped to a single cluster; use per-cluster signals in snapshot instead")
    snapshots = [_load_snapshot(path, None, args.prometheus) for path in (args.snapshot or [])]
    if args.all_contexts:
        if args.context:
            raise ValueError("--all-contexts and --context are mutually exclusive")
        from kubernetes import config
        contexts, _ = config.list_kube_config_contexts()
        if not contexts:
            raise RuntimeError("No kubeconfig contexts available")
        contexts = [x["name"] for x in contexts]
    else:
        contexts = args.context or []
    if contexts:
        from .collector import collect
        for context in contexts:
            try:
                snapshots.append(collect(context=context))
            except Exception as exc:
                snapshots.append({"schema_version": 2, "context": context, "nodes": [], "pods": [],
                                  "collection_errors": [f"context not collected: {type(exc).__name__}: {str(exc)[:160]}"]})
    if not snapshots:
        raise ValueError("Fleet/report requires at least one --snapshot, --context or --all-contexts")
    if args.signals:
        if len(snapshots) != 1:
            raise ValueError("--signals requires one cluster; include readings in each cluster snapshot otherwise")
        from .signals import load_signals
        snapshots[0].setdefault("signals", []).extend(load_signals(args.signals))
    return snapshots


def _emit(content, output):
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
        print(f"Wrote {target}", file=sys.stderr)
    else:
        print(content)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kdoctor", description="Evidence-driven Kubernetes Doctor (read-only by default)")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "explain", "topology", "snapshot", "plan"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--context", help="Kubeconfig context (default: active/in-cluster)")
        cmd.add_argument("--snapshot", help="Offline JSON instead of Kubernetes API")
        cmd.add_argument("--signals", action="append", help="Optional standardized telemetry JSON (repeatable)")
        cmd.add_argument("--prometheus", help="Optional read-only Prometheus endpoint for DCGM queries")
        cmd.add_argument("--output", "-o", help="Output path; default stdout")
        if command == "topology":
            cmd.add_argument("--format", choices=("json", "mermaid"), default="mermaid")
        elif command in ("scan", "explain"):
            cmd.add_argument("--format", choices=("json", "text"), default="text")
    for command in ("fleet", "report"):
        cmd = sub.add_parser(command, help="Aggregate multiple contexts; report creates self-contained HTML")
        cmd.add_argument("--snapshot", action="append", help="Repeatable: one snapshot per cluster")
        cmd.add_argument("--context", action="append", help="Repeatable: kubeconfig contexts")
        cmd.add_argument("--all-contexts", action="store_true", help="Scan all kubeconfig contexts")
        cmd.add_argument("--signals", action="append", help="Telemetry for exactly one cluster")
        cmd.add_argument("--prometheus", help="Prometheus for exactly one cluster")
        cmd.add_argument("--output", "-o", default="doctor-report.html" if command == "report" else None)
        if command == "fleet":
            cmd.add_argument("--format", choices=("json", "text"), default="text")
    fix = sub.add_parser("fix", help="Guarded manual Deployment restart; never autonomous")
    fix.add_argument("--context")
    fix.add_argument("--action", choices=("rollout-restart", "cordon-node", "uncordon-node"),
                     default="rollout-restart")
    fix.add_argument("--namespace")
    fix.add_argument("--deployment")
    fix.add_argument("--node", help="Node target for cordon-node or uncordon-node")
    mode = fix.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Default; no changes")
    mode.add_argument("--apply", action="store_true", help="Apply only with --yes --policy --ticket")
    fix.add_argument("--yes", action="store_true")
    fix.add_argument("--policy", help="Deny-by-default JSON policy allowlisting context and target")
    fix.add_argument("--ticket", help="Change ticket required when applying")
    fix.add_argument("--audit-file", default="./kdoctor-audit.jsonl")
    args = parser.parse_args(argv)
    try:
        if args.command == "fix":
            from .remediation import restart_deployment
            from .policy import load_policy
            if args.apply and (not args.policy or not args.ticket or not args.yes):
                raise PermissionError("Applying requires --apply --yes --policy PATH --ticket ID")
            policy = load_policy(args.policy) if args.policy else None
            if args.action == "rollout-restart":
                if not args.namespace or not args.deployment:
                    raise ValueError("rollout-restart requires --namespace and --deployment")
                result = restart_deployment(namespace=args.namespace, name=args.deployment,
                                            context=args.context, apply=args.apply, yes=args.yes,
                                            audit_path=args.audit_file, policy=policy, ticket=args.ticket)
            else:
                if not args.node:
                    raise ValueError(f"{args.action} requires --node")
                from .remediation import set_node_schedulable
                result = set_node_schedulable(name=args.node, context=args.context,
                                              schedulable=args.action == "uncordon-node",
                                              apply=args.apply, yes=args.yes, audit_path=args.audit_file,
                                              policy=policy, ticket=args.ticket)
            _emit(json.dumps(result, indent=2), None)
            return 0
        if args.command in ("fleet", "report"):
            snapshots = _fleet_snapshots(args)
            fleet = analyze_fleet(snapshots)
            if args.command == "report":
                from .dashboard import render_dashboard
                output = render_dashboard(fleet, snapshots)
            elif args.format == "json":
                output = json.dumps(fleet, indent=2)
            else:
                lines = [f"Kubernetes Doctor | {fleet['summary']['clusters']} clusters | "
                         f"{fleet['summary']['findings']} findings | {fleet['summary']['incidents']} incidents"]
                for c in fleet["clusters"]:
                    available = sum(g["unclaimed"] for g in c["gpu"] if g["ready"] and not g["tainted"])
                    lines.append(f"[{c['state']}] {c['context']}: nodes={c['node_count']} pods={c['pod_count']} "
                                 f"findings={len(c['findings'])} incidents={len(c['incidents'])} "
                                 f"GPU-unclaimed-on-ready-untainted-nodes={available} (not confirmed allocatable to workload)")
                    for incident in c["incidents"]:
                        lines.append(f"  -> {incident['resource']} | {incident['hypotheses'][0]['rule']} | "
                                     f"{' -> '.join(incident['hypotheses'][0]['path'])}")
                output = "\n".join(lines)
            _emit(output, args.output)
            return 2 if fleet["summary"]["incomplete"] else 0
        snap = _read_snapshot(args)
        if args.command == "snapshot":
            output = json.dumps(snap, indent=2)
        elif args.command == "topology":
            graph = topology(snap)
            output = to_mermaid(graph) if args.format == "mermaid" else json.dumps(graph, indent=2)
        else:
            findings = diagnose(snap)
            if args.command == "plan":
                output = json.dumps(propose_actions(snap, findings), indent=2)
            elif args.format == "json":
                output = json.dumps({"context": snap.get("context"),
                                     "complete": not snap.get("collection_errors"),
                                     "findings": findings, "incidents": correlate(snap, findings)}, indent=2)
            else:
                lines = [f"Kubernetes Doctor | context={snap.get('context')} | "
                         f"inventory={'INCOMPLETE' if snap.get('collection_errors') else 'COMPLETE'}",
                         f"Findings: {len(findings)} | Correlated incidents: {len(correlate(snap, findings))}"]
                for f in findings:
                    lines.extend(["", f"[{f['severity'].upper()}] {f['rule']} — {f['resource']}", f"  {f['summary']}"])
                    if args.command == "explain":
                        lines.extend(["  Evidence: " + "; ".join(e for e in f["evidence"] if e),
                                      "  Possible cause: " + f["possible_cause"],
                                      "  Recommendation: " + f["recommendation"]])
                output = "\n".join(lines)
        _emit(output, args.output)
        return 2 if snap.get("collection_errors") else (1 if args.command in ("scan", "explain") and diagnose(snap) else 0)
    except (ValueError, RuntimeError, PermissionError, OSError, ImportError) as exc:
        print(f"kdoctor: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"kdoctor: Kubernetes API or unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
