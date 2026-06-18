# Agent Session State / Brain Dump

**Objective:**
Write down exactly what you are currently doing, the last known successful step, and any immediate blockers.
This ensures the NEXT agent handling this repository knows exactly where you left off.

- **Current Task:** 
- **Last Action Taken:** 
- **Failing Tests / Errors:** 
- **Next Steps:** 

## 2026-06-18 Upgrade Center And AI-First Policy Gates

- **Current Task:** Add Admin UI upgrade readiness plus GitHub import, signal evidence, and policy-derived AI-first gates.
- **Last Action Taken:** Added `/api/admin/upgrade/status`, Upgrade Center UI, GitHub Actions/PR import into `ai_first_signals`, signal matching inside `build_evidence_pack`, Admin UI controls for GitHub import, `signal_evidence` Markdown output, readiness/evidence `policy_gates`, Admin policy gate rendering, and bumped README/backend metadata to `1.30.93`.
- **Failing Tests / Errors:** Focused tests passed: `python -m pytest src/tests/test_ai_first_api.py src/tests/test_mcp_tools.py src/tests/test_cga_relay_router.py -q`. `python -m compileall src/backend/ai_first src/backend/main.py`, Admin inline script syntax check, and `git diff --check` passed. Local `cga-relay sync` could not complete because the local CGA API returned `401 Not authenticated` for `/api/auth/me/groups`.
- **Next Steps:** Consider automatic Azure DevOps PR/build imports, PR template evidence links, and promoting warning gates into review gates for lighthouse repos.

## 2026-06-18 AI-First Project Collapse State

- **Current Task:** Make each project card on `/admin/ai-first` collapsible and remember the last collapsed state.
- **Last Action Taken:** Added per-project collapse state in `localStorage`, collapse controls, and collapsed-content styling in `src/frontend/index.html`; aligned README metadata with app version `1.30.88`.
- **Failing Tests / Errors:** No errors found in editor diagnostics; inline script syntax check passed; browser DOM validation confirmed per-project collapse and reload persistence. An initial direct CGA MCP indexing attempt used the wrong path and was corrected.
- **Next Steps:** Ready for review; rebuild was applied to the local desktop service at `http://localhost:18001`. CGA-Relay stdio MCP successfully queued incremental indexing for `/repos/ContextGraphAdmin` with job `bac84286-0fc8-4794-b2bf-eb517559da15` (`changed_count: 303`). That job is still pending because the earlier accidental `/repos` full-index job `5eb08404-c4f5-4f74-ab35-a84354f3e88b` is processing ahead of it.

## 2026-06-15 ADC Standards Refresh

- **Current Task:** Align ContextGraphAdmin ADC files with the latest local ADC template standards.
- **Last Action Taken:** Synced `.adc/standards`, added missing PR checklist, normalized rule pointer files, merged latest prompt-rule guidance while preserving local Docker and `cga-relay` paths, added a relay-first ContextGraph MCP profile, and bumped README/backend metadata to `1.30.84`.
- **Failing Tests / Errors:** No executable product tests were required for the documentation/configuration-only standards refresh.
- **Next Steps:** Review the ADC diff, then commit through the normal `dev/*` branch workflow if publishing this standards refresh.
