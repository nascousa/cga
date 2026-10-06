# Autonomous Development Constitution Notes

The Autonomous Development Constitution (ADC) is the project-context governance model used by CGA. It keeps architecture, conventions, domain knowledge, and AI instructions close to the code while separating them from ordinary product documentation.

## Integrated ADC Governance

CGA administrators manage the complete ADC document catalog at `/admin/adc`.
The bundled initial release is the locally available ADC **1.1.23** package
(44 documents), not a claim that upstream GitHub has no newer release. Its
provenance, publisher, publication timestamp and SHA256 are recorded. The
original package's document paths and content are retained.

- A published semantic version (`major.minor.patch`) is immutable, including
  at the database level. Stage document edits/additions/removals or import
  release JSON, then publish a **new** version with a reason. "Latest" means
  the greatest semantic version published in this CGA catalog, not the last
  historical version imported. No remote upstream polling is performed.
- New project creation and new registry imports atomically pin that latest
  release. Existing projects remain unbound until an administrator explicitly
  adopts a release; existing bindings never auto-upgrade.
- Each project revision references a release and stores its own changes:
  **amendment** adds a new project document, **override** replaces an existing
  baseline document, and **exemption** excludes a baseline document from the
  effective bundle without erasing the baseline or its history. All changes
  require a justification; exemptions can have a timezone-aware expiry.
- Revisions are append-only and record actor, reason, creation time, reviewed
  upgrade paths and rollback origin. Concurrent edits use an expected revision:
  stale writers receive HTTP 409 rather than overwriting someone else's work.
- Before an upgrade, preview the release diff and review affected project
  changes. Baseline changes underneath overrides/exemptions require explicit
  acknowledgement. Invalid overlay targets must be corrected, not silently
  dropped. Rollback creates a new revision; it does not delete history.
- Current effective content evaluates expiry at request time. A requested
  historical revision evaluates expiry at that revision's creation time.
  Rolling back an old revision does **not** reactivate an expired exemption.
- Downloaded ZIPs contain effective documents and `.adc/adc-lock.json` with
  release/hash, project revision, actor, reasons, change records and evaluation
  time. Archives are for review/apply through a project's normal source-control
  workflow: CGA does not overwrite repository files or execute imported scripts.
  In particular, review the original package's IDE/CI template locations
  before promoting them to root-level trigger files.

Authenticated account API:

| Endpoint | Purpose |
| --- | --- |
| `GET/POST /api/adc/releases` | List / publish releases (publish: admin only) |
| `GET /api/adc/releases/{id}` | Read all documents and provenance |
| `GET /api/adc/diff?from_release={id}&to_release={id}` | Document diffs |
| `GET /api/adc/projects/{id}` | Effective project ADC; optional `revision` |
| `GET /api/adc/projects/{id}/history` | Revision history |
| `POST /api/adc/projects/{id}/revisions` | Explicit adoption / upgrade / changes |
| `POST /api/adc/projects/{id}/restore` | Append a rollback revision |
| `GET /api/adc/projects/{id}/download` | Traceable effective ZIP; optional `revision` |

All project reads use CGA's existing group/project access rules. All writes
require an administrator. Existing project MCP tokens can retrieve their own
pinned ADC through `GET /api/project/adc` and `/api/project/adc/download`, using
the existing project-token middleware and header requirements. They cannot
publish releases, approve exceptions or read another project's ADC.

The release catalog and history live in PostgreSQL tables `adc_releases` and
`adc_project_revisions`, so they are included in existing database backups.
No graph schema migration or reindex is required. The complete seed is stored
in `src/backend/adc/seed.json`; the validated local release importer is
`src/scripts/import-adc-release.py`. Existing published seed content must not
be overwritten; later changes are new releases.

## Remote ADC Interfaces

From CGA/Relay 1.30.126, all three authenticated transports share the same
project-scoped service and immutable history:

- REST: `POST /api/project/adc/query` with `{"operation":"adc_current"}`.
- Direct MCP SSE: `/mcp/sse` (the `/mcp` discovery URL is not itself the SSE
  connection). Standard MCP clients can list and call the tools below.
- Desktop Relay stdio: `cga-relay mcp --config <project.env>`, forwarding through
  `/api/project/cga-relay/mcp-tool` or the account-JWT bridge
  `/api/auth/cga-relay/mcp-tool`.

| Tool | Parameters | Result |
| --- | --- | --- |
| `adc_catalog` | `offset=0`, `limit=50` (1..100) | Published versions; highest semantic version |
| `adc_release` | `release_id` | Immutable baseline documents and provenance |
| `adc_current` | Optional `revision` | Project binding, effective documents and exceptions |
| `adc_history` | `offset=0`, `limit=50` | This project's append-only revisions |
| `adc_diff` | `release_id` | Proposed baseline differences and affected overlay paths |
| `adc_document` | `path`, optional `revision` | Effective content, SHA256 and evaluation time |
| `adc_bundle` | Optional `revision` | Files with content/hash, project identity and provenance lock |
| `adc_sync` (local Relay only) | `apply=false` | Preview/apply the approved bundle to configured checkout |

REST uses the same parameter names alongside `operation`. Project-token calls
require `Authorization: Bearer <token>` and the configured external project ID
in `X-Project-ID`. Include the existing communication-profile headers:

```text
X-CGA-Communication-Profile: CRYSTALS-CNSA-2.0
X-CGA-Key-Establishment: ML-KEM-1024
X-CGA-Signature: ML-DSA-87
X-CGA-Transport-Scope: local-ipc
```

These are compatibility/policy declarations, **not an implementation or proof
of post-quantum cryptography**. Relay sends them for its loopback HTTP hop.
Remote network hops must use a separately authenticated, certificate-verified
secure tunnel/proxy. Do not send project/account credentials over LAN plaintext.
The Rust client intentionally rejects non-loopback HTTP; remote direct SSE
clients also need secure transport and an allowed server Host/Origin.

Project credentials cannot publish, adopt, upgrade, roll back or approve their
own exceptions. Those remain administrator operations in CGA's UI/account API.
Existing unbound projects receive 409 for bundles/documents until an administrator
adopts a baseline. Catalog availability is not approval to install the latest
version. Explicit historical bundles are marked `historical` and Relay will not
auto-install them. Responses above 7 MiB receive an explicit 413, not truncated
content; read individual documents, paginate history/catalog, or use the ZIP
download endpoint for larger exports.

Local synchronization checks configured project identity, SHA256, safe document
paths, case collisions and symlink/reparse-point ancestors before writing.
It never executes downloaded documents. Preview is the default; `--apply` is
required for CLI writes. Modified managed files and conflicting untracked files
block **all** planned writes; unrelated untracked files are retained. Approved
removals/exemptions remove only previously managed, unchanged files.

Relay retains checkpoints and per-operation backups in
`STATE_DIR/adc-<project-and-root-hash>/`. Normal validation conflicts make no
changes. I/O failures or process interruption during apply can leave a partial
tree: `pending.json` explicitly blocks subsequent sync rather than claiming
success. Preserve that journal and its backup directory; restore each recorded
path from its numeric backup (or remove a newly created path whose `existed`
flag is false), verify the tree against the previous `applied.json`, and only
then clear the pending journal/staging file. Do not discard local work during
recovery. Per-file replacements are atomic, not a filesystem-wide transaction.

Neither server nor Relay requires an independent ADC checkout: seed documents,
published releases, overlays and history are owned by CGA and its PostgreSQL
backup. Keep the old ADC project until all consuming projects have explicitly
adopted CGA governance and their local edits have been reviewed.

## Purpose (Background)

ADC exists to help AI coding agents and human developers acquire accurate project context quickly. It reduces the chance that an agent writes code that violates local conventions or misses historical architecture decisions.

CGA is designed to work well with ADC-style repositories because CGA indexes the code and exposes graph-aware retrieval, while ADC defines the rules, vocabulary, and workflows that agents should follow.

## Repository Boundary

An ADC-enabled repository keeps governance context in a hidden `.adc/` directory at the project root.

```text
project-root/
├── .adc/
├── src/
├── docs/
├── tests/
└── ...
```

Important boundaries:

- `.adc/` is internal AI governance and context.
- `docs/` is user-facing, API, and project documentation.
- `src/`, `docs/`, `tests/`, and other application folders remain root-level siblings of `.adc/`.
- Application source and public documentation should not be placed inside `.adc/`.

## Core ADC Files

```text
.adc/
├── index.md
├── bootstrap.md
├── prompt-rules.md
├── planning/
│   ├── status.md
│   ├── project-roadmap.md
│   └── development-phases.md
├── standards/
│   ├── conventions/
│   ├── checklists/
│   └── runbooks/
├── knowledge/
│   ├── glossary.md
│   ├── known-issues.md
│   ├── amendments.md
│   ├── adr/
│   └── diagrams/
└── cga-relay/
    ├── tasks/
    ├── scratchpad/
    ├── mcp/
    └── skills/
```

### `index.md`

The main context entry point. It should include structured metadata, project background, core modules, and environment requirements.

```yaml
---
project-name: "Your Project Name"
version: "1.0.0"
description: "A concise description of the project's core business value."
tech-stack:
  - React 18
  - Node.js 20
  - PostgreSQL
architecture-style: "Microservices / Monolith / Event-Driven"
entry-points:
  - src/main.ts
---
```

### `prompt-rules.md`

The mandatory AI instruction layer. This file captures strict rules such as coding conventions, security constraints, test expectations, and context-loading requirements.

### `bootstrap.md`

The exact commands needed to install dependencies, start local services, run databases, and launch development servers.

### `planning/`

Planning documents keep agents aligned with current phase, roadmap, active goals, and recent major changes.

### `standards/conventions/`

Conventions are split by domain so agents can load only the relevant rules for a task. Common domains include frontend, backend, data engineering, performance, observability, security, DevOps, testing, and structure.

### `knowledge/`

Knowledge documents preserve terminology, known issues, no-touch zones, architecture decisions, amendments, and living diagrams.

### `cga-relay/`

This workspace is for relay orchestration state, MCP wiring, scratchpad notes, task queues, and specialized skills. Canonical requirements and architecture decisions should remain in planning, standards, and knowledge files.

## Agent Initialization Protocol

ADC-aware agents should follow this high-level order before making non-trivial changes:

1. Read `.adc/index.md`, `.adc/planning/status.md`, and `.adc/planning/development-phases.md`.
2. Read `.adc/knowledge/known-issues.md` before planning refactors.
3. Read `.adc/prompt-rules.md` and follow mandatory conventions.
4. Read `.adc/knowledge/glossary.md` for domain-specific names and acronyms.
5. Check `.adc/cga-relay/skills/` for project-specific workflows.
6. Check `.adc/cga-relay/mcp/` for MCP server wiring.
7. Complete relevant `.adc/standards/checklists/` before finalizing commits or pull requests.
8. Update living Mermaid diagrams when architecture, data flow, or schema changes.

## ContextGraph And MCP Policy

ADC-compliant projects can provide a preconfigured `cga-mcp-server` entry in `.adc/cga-relay/mcp/mcp-servers.json` so agents can load CGA retrieval tools consistently.

For local CGA development, the default SSE MCP endpoint is:

```text
http://localhost:18001/mcp/sse
```

Use `Authorization` and `X-Project-ID` headers when project-scoped access is required.

ContextGraph MCP integrations are for retrieval, indexing, and external context operations. Local build, test, and deployment execution should remain on native project tooling.

ContextGraph credentials such as `CONTEXTGRAPH_PROJECT_ID` and `CONTEXTGRAPH_MCP_TOKEN` must be injected through environment variables and must not be committed.

## Governance Patterns

- Store project vocabulary in `knowledge/glossary.md` so agents use correct names in code and docs.
- Store historical decisions in `knowledge/adr/` so agents avoid re-proposing rejected architectures.
- Store technical debt and no-touch zones in `knowledge/known-issues.md`.
- Store architecture and data-flow diagrams in Mermaid under `knowledge/diagrams/`.
- Treat changes to ADC rules as governance changes that require human review.

## Quick Start Skeleton For New ADC Repositories

The following command creates a bare ADC skeleton for an existing codebase. Populate the files with the actual project rules before relying on them for agent automation.

```bash
mkdir -p .adc/planning .adc/standards/conventions .adc/standards/checklists .adc/standards/runbooks .adc/knowledge/adr .adc/knowledge/diagrams .adc/cga-relay/skills .adc/cga-relay/mcp .adc/cga-relay/tasks/todo .adc/cga-relay/tasks/in-progress .adc/cga-relay/tasks/done .adc/cga-relay/scratchpad tests .github
touch .adc/index.md .adc/bootstrap.md .adc/prompt-rules.md .adc/planning/status.md .adc/planning/project-roadmap.md .adc/planning/development-phases.md .adc/knowledge/glossary.md .adc/knowledge/known-issues.md .adc/knowledge/amendments.md .adc/standards/conventions/structure.md .adc/standards/conventions/frontend.md .adc/standards/conventions/backend.md .adc/standards/conventions/data-engineering.md .adc/standards/conventions/performance.md .adc/standards/conventions/observability.md .adc/standards/conventions/security.md .adc/standards/conventions/devops.md .adc/standards/conventions/testing.md .adc/cga-relay/mcp/mcp-servers.json .adc/standards/checklists/pr-review.md .adc/standards/runbooks/001-common-errors.md .adc/cga-relay/scratchpad/session.md .adc/cga-relay/tasks/todo/TASK-001.md .adcignore .cursorrules .windsurfrules .clinerules .roomadesrules .aider.rules .codexrules .antigravityrules .codeiumrules .codyrules .github/copilot-instructions.md
```