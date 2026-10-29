# Security and operational safety

Kubernetes Doctor is a prototype. It is **read-only by default** and must not be treated as an autonomous production repair controller.

- Do not commit kubeconfig files, cluster inventories, internal metrics, audit logs, credentials or real customer telemetry.
- The provided fixtures and dashboard are synthetic. Live snapshots may still contain sensitive identifiers or incident details.
- Run with the minimum read-only Kubernetes RBAC from `deploy/rbac-readonly.yaml`.
- Mutation commands require explicit `--apply --yes`, an allowlisted policy, a ticket reference, RBAC authorization and precondition checks.
- A successful patch API call does **not** establish that workload health has recovered.
- Review code and run validation against a disposable cluster before production use.

If you find a security issue, contact the repository maintainer privately rather than publishing live credentials or exploit data in an issue.
