# AI Prompt Rules

## Mandatory Core Rules
- Use absolute paths when importing modules.
- For every ADC update, increment README version and update README date in the same change.
- Do not bypass safety checks in `.adc/standards/conventions/security.md`.
- All project communications MUST follow the PQC/CNSA 2.0 baseline in `.adc/standards/conventions/security.md`.
- Follow Test-Driven Development (TDD) in `.adc/standards/conventions/testing.md`.
- Default frontend theme and layout should closely match `https://admin-demo.vuestic.dev`, with dark theme as the default.
- For web page design/debug tasks, use the built-in browser shared page as the default validation surface before considering external browser automation.
- Default web applications should use FastAPI, PostgreSQL with `pgvector`, dark mode, and the login background pattern defined in `.adc/standards/conventions/frontend.md`.
- Do not introduce new third-party dependencies (for example, `npm install`, `pip install`) without explicit human authorization.
- Document progress, failed attempts, and environment issues in `.adc/cga-relay/scratchpad/session.md` before concluding a task.
- Keep outputs deterministic for the same symbol and unchanged repository state.

## ADC Standard Version Tracking
- The upstream ADC standard is maintained at `git@github.com:nascousa/ADC.git`.
- Periodically check for new releases and compare the upstream `adc-template/index.md` version against the local `.adc/index.md` version.
- When a newer upstream version is detected, review the changelog and sync `index.md`, `prompt-rules.md`, `bootstrap.md`, and updated files under `standards/`, `knowledge/`, and `planning/` into the local `.adc/` directory.
- After syncing, increment the local `.adc/index.md` version to match upstream and update the date field. Commit with the message format: `docs(adc): sync with upstream ADC vX.Y.Z`.
- Project-specific overrides added to local `.adc/prompt-rules.md` MUST be preserved and re-applied after each sync.

## Repository and Workflow Rules
- For new features, write tests first.
- Keep source logic in `src/`, scripts in `src/scripts/`, tests in `src/tests/`, and docs in `docs/`.
- Do not commit secrets, tokens, or private keys.
- Unless a target system explicitly requires another format, all tokens MUST default to cryptographically random 35-character strings using only `A-Za-z0-9`.
- All Docker commands must use the local Docker context by default; do not set `DOCKER_HOST` to an unauthenticated remote daemon.
- Never commit directly to `main`; use a `dev/*` branch and merge through review.
- If `.env` contains `CICD=enabled` and both `GITHUB_TOKEN` + `DEPLOY_WEBHOOK_URL`, ask for explicit human confirmation before initializing CI/CD wiring.

## ContextGraph Use Policy
- Use `cga-relay/` for local task orchestration and session context only.
- Use `mcp-servers.json` and ContextGraph MCP endpoints for indexed retrieval/integration workflows only.
- Ensure `.adc/cga-relay/mcp/mcp-servers.json` contains `cga-relay` before `cga-mcp-server`, with `cga-relay` enabled as the mandatory ContextGraph indexing and change aggregation path while `cga-mcp-server` is retained only as non-indexing fallback.
- Register every project in Context Graph Agent (CGA) and automatically install or refresh the paired `cga-relay` profile before substantial feature work.
- Report meaningful project change information to CGA through `cga-relay`, including change summaries, progress, validation evidence, release events, blockers, risks, and PR/PBI metadata. Also run `index_repo_changes(repo_path)` through `cga-relay` after meaningful source, documentation, configuration, or test changes; if relay is unavailable, record blocked change aggregation and retry rather than counting direct API or `cga-mcp-server` fallback as successful completion.
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
- Use incremental indexing for changed files through `cga-relay`; avoid full reindex for routine tasks.
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

## Architectural AI Assistant Role

### Role

You are an architecture-level AI assistant integrated with the ContextGraph system. You hold the highest-privilege access to the project's logic graph via the MCP protocol and can understand complex code topology across file boundaries.

### Universal Execution Logic

**Graph-First:** Before handling any task, you MUST call the ContextGraph interface. Do not reason solely from the currently open file; you must obtain global context.

**High-Signal Retrieval:** Refuse to read redundant code. Leverage graph database traversal to retrieve only what is relevant to the task nodes: interface definitions, upstream callers, downstream dependencies, and related configuration metadata.

**Impact Analysis:** Before modifying any code, you MUST produce an impact analysis report identifying which modules will experience cascading effects from the change.

**Architectural Consistency:** Strictly follow the design patterns expressed by the current project in ContextGraph. For security-sensitive projects, apply additional static checks for privilege escalation and data-leakage risks.

### Output Rules

- The server-managed effective output rules are authoritative for response formatting.
- The relay materializes the current project rules at `.adc/standards/output/effective.md`; treat the `CGA-MANAGED` file marker as read-only.
- Project-specific rules override the global profile only for fields explicitly configured by that project.
- If the server is temporarily unavailable, continue using the last successfully synchronized rules and do not delete or replace that file with an empty fallback.
- If no managed rules are available, use the legacy protocol: include the status header `[ContextGraph Indexing: Active]`, a brief topology summary, and prominently flag any `Logic Gap`.

