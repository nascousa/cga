# Source Layout

This folder is reserved for the Azure Policy Monitor extension implementation.

Planned structure:

```text
src/
  policy_monitor/
    __init__.py
    config.py
    scanner.py
    runner.py
    reports.py
    cga_adapter.py
```

The scanner core should stay independent from CGA-specific APIs. CGA integration should live in adapter code so the extension can remain easy to test and, if needed later, move to a separate package or repository.
