# AI Prompt Rules

## Mandatory Core Rules
- Use absolute paths when importing modules.
- For every ADC update, increment README version and update README date in the same change.
- Do not bypass safety checks in `.adc/standards/conventions/security.md`.
- Follow Test-Driven Development (TDD) in `.adc/standards/conventions/testing.md`.
- Default frontend theme and layout should closely match `https://admin-demo.vuestic.dev`, with dark theme as the default.
- Do not introduce new third-party dependencies (for example, `npm install`, `pip install`) without explicit human authorization.
- Document progress, failed attempts, and environment issues in `.adc/contextgraph-edge-agent/scratchpad/session.md` before concluding a task.
- Keep outputs deterministic for the same symbol and unchanged repository state.

## Repository and Workflow Rules
- For new features, write tests first.
- Keep source logic in `src/`, scripts in `src/scripts/`, tests in `src/tests/`, and docs in `docs/`.
- Do not commit secrets, tokens, or private keys.
- Unless a target system explicitly requires another format, all tokens MUST default to cryptographically random 35-character strings using only `A-Za-z0-9`.
- All Docker commands must use the local Docker context by default; do not set `DOCKER_HOST` to an unauthenticated remote daemon.
- Never commit directly to `main`; use a `dev/*` branch and merge through review.
- If `.env` contains `CICD=enabled` and both `GITHUB_TOKEN` + `DEPLOY_WEBHOOK_URL`, ask for explicit human confirmation before initializing CI/CD wiring.

## ContextGraph Use Policy
- Use `contextgraph-edge-agent/` for local task orchestration and session context only.
- Use `mcp-servers.json` and ContextGraph MCP endpoints for indexed retrieval/integration workflows only.
- Ensure `.adc/contextgraph-edge-agent/mcp/mcp-servers.json` contains the `cga-mcp-server` entry and keep it enabled by default for ADC-compliant repositories.
- Do not assume a Node-specific local MCP bootstrap; prefer endpoint-first MCP profiles and keep integration language/runtime-agnostic unless the repository explicitly provides a local server implementation.
- ContextGraph MCP must not replace local compile, lint, unit test, or integration test execution.
- Treat scratchpad/task outputs as operational context, not canonical product truth.
- Canonical rules must remain in `.adc/planning/`, `.adc/standards/`, and `.adc/knowledge/`.
- Inject `CONTEXTGRAPH_MCP_TOKEN`, `CONTEXTGRAPH_EDGE_AGENT_TOKEN`, and `CONTEXTGRAPH_PROJECT_ID` via environment variables only.
- Never write ContextGraph credentials into tracked files.
- PRs changing ContextGraph integration behavior must update `.adc/bootstrap.md` and MCP server wiring, and include validation notes.

## ContextGraph Retrieval and Token Policy
- For non-trivial coding tasks, perform ContextGraph retrieval before editing files.
- Prefer FalkorDB Cypher traversal over Python loops for impact graph search.
- Required pre-edit sequence: `contextgraph_index_incremental` -> `contextgraph_query_impact_graph` -> `get_optimized_context` -> `contextgraph_fetch_minimal_code`.
- Use incremental indexing for changed files; avoid full reindex for routine tasks.
- Retrieve context in order: impact graph -> optimized context -> minimal code.
- Use symbol-scoped or change-scoped queries; avoid whole-repository prompts.
- Start with conservative budgets (`800-1500`) and expand only when evidence is insufficient.
- Apply explicit `token_budget` limits and keep only direct dependencies, recent changes, and high-frequency call paths.
- Reuse previously selected minimal context across related follow-up questions instead of re-fetching broad context.
- If ContextGraph evidence is missing, report missing symbols/files first, then run one bounded fallback search.
- Keep answers evidence-first by citing minimal retrieved code context before proposing broad refactors.

## ContextGraph-First Trigger Conditions
- Cross-module changes.
- Noisy repository search or ambiguous ownership.
- Runtime errors where call chain/source owner is unclear.
- Requests expected to exceed a small context window.

## Allowed Exceptions
- Single-line edits with exact file and line already known.
- Pure formatting or comment-only updates.
- Emergency hotfixes where retrieval failure blocks immediate mitigation.


