# Source Layout

This folder contains the dependency-free Azure Policy Change Monitor implementation.

```text
src/
  policy_monitor/
    __init__.py
    azure_auth.py       # Environment, workload, managed identity, and Azure CLI tokens
    azure_rest.py       # Read-only Azure Management REST transport
    azure_state.py      # Stable normalization, redaction, and snapshot construction
    diff.py             # Deterministic deployed-state drift findings
    notifications.py    # Severity-gated webhook and SMTP adapters
    runner.py           # Repository/Azure orchestration and optional output boundary
    scanner.py
    summary.py          # Evidence-grounded optional model output
```

The package does not import CGA backend modules. CGA integration, persistence, scheduling, and runtime SMTP configuration live under `src/backend/extensions/`. This boundary keeps collection and drift logic independently testable.

The package must remain read-only. Add Azure operations only when they collect evidence, and keep all policy decisions deterministic rather than delegating them to the optional model adapter.
