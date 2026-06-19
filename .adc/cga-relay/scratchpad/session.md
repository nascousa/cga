# Agent Session State / Brain Dump

**Objective:**
Write down exactly what you are currently doing, the last known successful step, and any immediate blockers.
This ensures the NEXT agent handling this repository knows exactly where you left off.

- **Current Task:** 
- **Last Action Taken:** 
- **Failing Tests / Errors:** 
- **Next Steps:** 

## 2026-06-18 Upgrade Center, AI-First Evidence, And FalkorDB Persistence

- **Current Task:** Add Admin UI upgrade readiness, CI/PR signal imports, PR evidence links, policy-derived AI-first gates, and persistent FalkorDB volume mounts.
- **Last Action Taken:** Added `/api/admin/upgrade/status`, Upgrade Center UI, GitHub and Azure DevOps import into `ai_first_signals`, signal matching inside `build_evidence_pack`, Admin UI controls for imports, saved evidence PR-template export, `signal_evidence` Markdown output, readiness/evidence `policy_gates`, Admin policy gate rendering, FalkorDB compose volume fixes, and bumped README/backend/docs metadata to `1.30.95`.
- **Failing Tests / Errors:** Final focused verification passed after the `1.30.95` increment: `pytest src/tests/test_ai_first_api.py src/tests/test_mcp_tools.py src/tests/test_cga_relay_router.py -q` (67 passed), `python -m compileall src/backend/ai_first src/backend/main.py src/tests/test_ai_first_api.py`, Admin inline script syntax check, and `git diff --check`. This CPMD pass also passed `python -m pytest src/tests/test_ai_first_api.py src/tests/test_health.py src/tests/test_template_quality.py -q --no-cov`, `python -m compileall src/backend/ai_first src/backend/main.py`, Admin inline script syntax check, editor diagnostics, and `git diff --check`. Local live smoke passed for `/health` (`1.30.95`), `/admin/ai-first` ADO/PR UI, and ADO endpoint registration. Local `cga-relay sync` remains blocked by expired account auth: `/api/auth/cga-relay/sync` returns `401 Unauthorized`, and `CGA_API_KEY` / `CGA_DEVELOPER_TOKEN` are not configured. Relay dry-run is healthy after local excludes were narrowed (`356` scanned files, `84520` excluded).
- **Next Steps:** Code was committed and pushed as `f88ce64` on `dev/readme-docs-restructure-20260603`. Re-run `cga-relay sync --config %USERPROFILE%\.cga\relay.env --namespace account --project-tag ContextGraphAgent` after refreshing local relay login or setting `CGA_DEVELOPER_TOKEN`.

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
