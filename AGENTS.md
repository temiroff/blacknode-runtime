# blacknode-runtime Agent Instructions

This independent package owns authenticated remote deployment, runtime
manifests, artifact staging, process supervision, logs, and rollback.

Keep physical hardware access in `blacknode-hardware` and planning, memory, and
skill selection in `blacknode-agent`. Runtime deployments may call those
services through their public contracts but must not duplicate them.

Safety and security:

- Authentication is mandatory for manifest and deployment endpoints.
- Never write token values to logs, process arguments, configuration, Git, or
  deployment artifacts.
- Staging never starts a deployment; start is a separate explicit operation.
- Stop must be idempotent and terminate the complete deployment process group.
- Preserve previous revisions for explicit rollback.
- Reject malformed, oversized, or non-compiling Python artifacts.
- Unit tests do not establish physical hardware, firewall, or internet safety.

Run package tests with:

```powershell
python -m pytest tests
```
