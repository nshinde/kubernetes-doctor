#!/usr/bin/env bash
# Run from repository root. Offline synthetic case study; does not contact a cluster.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
A="examples/reactor-demo/us-west.json"
B="examples/reactor-demo/us-east.json"
C="examples/reactor-demo/eu-central.json"
mkdir -p demo-output
printf '\n=== 1/5: Evidence-driven single cluster triage ===\n'
python -m kubernetes_doctor.cli explain --snapshot "$A" || test "$?" -eq 1
printf '\n=== 2/5: Cross-cluster comparison ===\n'
python -m kubernetes_doctor.cli fleet --snapshot "$A" --snapshot "$B" --snapshot "$C"
printf '\n=== 3/5: Topology export ===\n'
python -m kubernetes_doctor.cli topology --snapshot "$A" --format mermaid -o demo-output/topology.mmd
printf '\n=== 4/5: Operator-only corrective action suggestions ===\n'
python -m kubernetes_doctor.cli plan --snapshot "$A" -o demo-output/recovery-plan.json
printf '\n=== 5/5: Self-contained HTML dashboard ===\n'
python -m kubernetes_doctor.cli report --snapshot "$A" --snapshot "$B" --snapshot "$C" -o demo-output/reactor-dashboard.html
printf '\nOpen demo-output/reactor-dashboard.html in your browser. No live clusters were touched.\n'
