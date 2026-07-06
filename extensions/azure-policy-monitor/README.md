# Azure Policy Monitor Extension

This extension contains the Azure Policy Change Monitor for CGA.

## Layout

```text
extensions/azure-policy-monitor/
  src/   # Extension implementation: scanner, runner, adapters, reports
  docs/  # Extension design notes, source extraction, integration plan
```

## Intent

The extension monitors Azure Policy repository changes and deployed Azure Policy drift. It is designed to run as a CGA project-level extension and to be triggered by CGA Schedule.

## Boundary

- CGA owns projects, auth, schedule execution, run history, and admin UI.
- This extension owns policy scanning, Azure Policy checks, findings, and report generation.
- Schedule should call the extension through a stable extension contract rather than embedding policy-specific logic directly in the scheduler.
