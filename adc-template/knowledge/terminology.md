# Project Terminology & Abbreviations

This file defines all domain-specific terminology, abbreviations, and shortcuts used in this project and the ADC framework.

## Development Workflow Abbreviations

| Abbreviation | Full Term | Definition |
|---|---|---|
| **CPMD** | Checkin/Push/Merge/Deploy | Four-step git workflow: `git add -A`, `git commit -m "..."`, `git push origin main`, then deploy via CI/CD pipeline |
| **TDD** | Test-Driven Development | Development methodology: write failing tests first, then implement to pass, then refactor with tests passing |
| **ADC** | Agent Development Constitution | Standardized framework for managing project governance, AI instructions, and developer context in `.adc/` folder |
| **GHC** | GitHub Copilot | Standard abbreviation for GitHub Copilot in this project documentation and discussion |
| **MCP** | Model Context Protocol | Protocol for AI agents (like Copilot, Claude) to access external tools and data sources |
| **ContextGraph** | ContextGraph | Central repository and artifact management system; use `ContextGraph` as shorthand in code/docs |

## ContextGraph Components

| Term | Definition |
|---|---|
| **ContextGraph** | Central artifact catalog, project registry, and orchestration service; local CGA Admin UI runs at `http://localhost:18001/admin` |
| **CGA MCP Server** | Model Context Protocol endpoint for programmatic ContextGraph access; default local dev SSE endpoint is `http://localhost:18001/mcp/sse` |
| **CGA-Relay** | Local MCP gateway and project sync agent; workspace stored in `.adc/cga-relay`, implementation in `src/cga-relay` |
| **CGA MCP Server Profile** | Standard MCP server profile name (`cga-mcp-server`) used by ADC templates for auto-loading CGA MCP wiring |
| **CGA Admin UI** | Local setup surface for project registration and token creation at `http://localhost:18001/admin` |

## Project Structure Terms

| Term | Definition |
|---|---|
| **.adc/** | Hidden "Agent Development Constitution" directory at project root containing governance, AI instructions, and agent workspace |
| **src/** | Source code directory containing all application logic, utilities, and build scripts |
| **src/dist/** | Compiled/bundled output directory with subdirs: `release/` (production artifacts), `staging/` (pre-prod), `build/` (cache) |
| **docs/** | User-facing, publishable project documentation (separate from `.adc/`) |
| **tests/** | Isolated test directory mirroring `src/` structure; never mix with source files |
| **.env** | Environment configuration file (git-ignored); never commit real secrets |
| **.env.example** | Template with dummy values showing all required environment variables (committed to git) |

## ADC Subdomains

| Subdomain | Location | Purpose |
|---|---|---|
| **Planning** | `.adc/planning/` | Project phases, roadmap, status tracking |
| **Standards** | `.adc/standards/` | Conventions, checklists, runbooks organized by domain |
| **Knowledge** | `.adc/knowledge/` | Glossary, known issues, ADRs, diagrams |
| **CGA-Relay** | `.adc/cga-relay/` | Relay workspace: tasks, scratchpad, MCP configs, skills |

## File Type Conventions

| Abbreviation | Full Name | Usage |
|---|---|---|
| **.md** | Markdown | Documentation, guidelines, decisions |
| **.mmd** | Mermaid | Diagram format for architecture, data flow, state machines |
| **.json** | JSON | Configuration files (mcp-servers.json, package.json) |
| **ADR** | Architecture Decision Record | Historical record of why architectural choices were made |

## Git & Deployment

| Term | Definition |
|---|---|
| **Commit** | Git snapshot of code changes with descriptive message |
| **Push** | Send committed changes from local to remote repository (origin/main) |
| **Merge** | Integrate changes from one branch into another; main branch is primary |
| **Deploy** | Release code to production via CI/CD pipeline triggered after push to main |
| **origin/main** | Primary branch on remote repository; commits here trigger deployments |

## Testing & Quality

| Term | Definition |
|---|---|
| **Unit Test** | Test of single function/module in isolation |
| **Integration Test** | Test of multiple components working together |
| **E2E Test** | End-to-end test simulating real user workflows |
| **Coverage** | Percentage of code lines executed by tests; target 80%+ for core logic |
| **Mock** | Simulated object/service used in tests instead of real implementation |

## Quality Glossary

| Term | Definition |
|---|---|
| **Branch Coverage** | Percentage of decision branches (`if/else`, match/case, boolean paths) exercised by tests |
| **Changed-Lines Coverage** | Coverage measured only on lines modified in the current change set |
| **Mutation Score** | Percentage of injected code mutations that are detected (killed) by tests |
| **Flaky Test Rate** | Ratio of tests that fail nondeterministically without code changes |
| **Golden Test** | Regression test comparing generated output against approved baseline artifacts |
| **Smoke Test** | Small, fast test set validating critical system behavior after changes |

## Security & DevOps

| Term | Definition |
|---|---|
| **CVE** | Common Vulnerabilities and Exposures; tracked severity score |
| **CVSS** | Common Vulnerability Scoring System; 0-10 scale (threshold: 7.0+) |
| **Secret Manager** | Secure storage for credentials (AWS Secrets Manager, HashiCorp Vault) |
| **.gitignore** | File specifying paths Git should NOT commit (build artifacts, .env, node_modules) |
| **Container** | Docker containerized application environment with resource limits |

## Observability & Logging

| Term | Definition |
|---|---|
| **Structured Logging** | JSON logs with consistent schema including event_id, timestamp, user_id |
| **Distributed Tracing** | OpenTelemetry context propagation across services for end-to-end request tracking |
| **Metrics** | Quantitative measurements (e.g., `orders_processed_total`) tracked over time |
| **Telemetry** | Automated collection of system behavior data for monitoring and debugging |


