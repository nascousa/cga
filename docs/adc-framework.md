# Agent Development Constitution Notes

The Agent Development Constitution (ADC) is the project-context governance model used by CGA. It keeps architecture, conventions, domain knowledge, and AI instructions close to the code while separating them from ordinary product documentation.

ADC means **Agent Development Constitution**. The earlier expansion is retired
for current documentation, templates and new master releases. Immutable older
releases and pinned adoption snapshots retain their original wording for
provenance; the naming correction does not silently upgrade existing projects.

The local CGA master catalog published this terminology-only correction as
**ADC 1.2.2** on 2026-11-10, derived from 1.2.1. Twelve documents changed; all
three earlier releases and existing project revisions were verified unchanged.
Projects can review and explicitly adopt 1.2.2 through **Amendments**.

### Inline Information And Rich Tooltips

The central master **ADC 1.2.3** adds the mandatory **Inline Information and
Rich Tooltips** section to `.adc/standards/conventions/frontend.md`. Any
explanation that is not clear in one short, single-line sentence must move into
an info-icon tooltip. The icon is immediately to the right of its associated
text, on the same line. Rich text must retain meaningful paragraphs, emphasis,
lists, code and line breaks; a native plain-text `title` is not sufficient.

Help must support hover, focus and click/touch, accessible naming and description,
Escape dismissal, hoverable/selectable content, viewport containment and active
theme colors. Essential errors/actions remain visible as concise summaries.
Untrusted content must not be interpreted as HTML.

The ADC interface uses structured DOM content (bold headings and lists) for
inline help, including governance, provenance, editing/import, adoption and
collection explanations. Tooltip nodes/listeners are disposed on sign-out.
Info circles are visually 14px, inside transparent 24px buttons that retain
usable pointer/touch targets without enlarging the displayed icon.
Master 1.2.3 changes only this frontend policy document; earlier releases and
all existing project bindings/revisions remain unchanged. Adoption is explicit.

### Fixed Project Ports And Conflict Cleanup

The central master **ADC 1.2.4**, published on 2026-11-11, adds **Fixed Project
Ports and Conflict Cleanup** to `.adc/standards/conventions/devops.md` and a
mandatory instruction in `.adc/prompt-rules.md`. Development, previews, UI
validation, test servers and deployments must retain the configured ports and
original URL. Healthy existing instances are reused, not duplicated on another
port. Port changes require explicit user approval.

On conflict, identify the exact listener, project ownership and startup source.
Stop/remove only a confirmed obsolete same-project instance by its PID, service
or container ID, removing obsolete auto-start registrations when necessary and
preserving data, configuration and recovery. Never kill a shared Docker host
process or unrelated service. Uncertain ownership or missing permissions is a
blocker, not permission to choose another port. Verify health at the original URL.

Only these two master documents changed; the other 43 documents, five prior
releases and existing project bindings/revisions were verified unchanged.
The [automatically loaded project instruction](../.github/instructions/runtime-ports.instructions.md)
enforces this rule immediately for CGA on port **18001**, without rewriting its
approved adoption snapshot or silently upgrading other projects.

## Integrated ADC Governance

CGA administrators manage the complete ADC document catalog at `/admin/adc`.
The manager has three independent subtabs:

- **Constitution** places a narrow, searchable document menu on the left and
  the document editor on the right. A compact release/provenance toolbar replaces
  stacked headings; document path and staging actions sit above the editor.
  **Publish & import** is collapsed by default, with JSON import nested inside.
  The selected document is highlighted;
  filtering retains the current editor draft. Switching documents asks before
  discarding unstaged edits. Stage documents before publishing a new immutable
  Constitution release. Narrow screens stack the menu above the editor.
  Historical Windows line endings are normalized only when comparing editor
  drafts, avoiding false unsaved-change prompts without rewriting release data.
- **Amendments** selects a project and manages its pinned baseline and changes,
  history, rollback and rule collection from registered checkouts.
  A compact project/summary/download toolbar leads to a left-side change picker
  and right-side editor. Path/type and justification/expiry share rows on desktop.
  Staging actions and the revision reason/save bar stay beside the editing flow.
  **Baseline & review**, **Revision history** and **Collect project rules** are
  collapsed by default, keeping low-frequency tools out of the main form.
  Inspecting history opens the review output; collapsing tools retains their
  DOM values and drafts. Baseline preview/acknowledgement remains required for
  upgrades even when its tools are collapsed. Small screens stack the columns.
  The label covers existing amendment, override and exemption types; it does
  not change their semantics or rename stored project revisions.
- **Versions** shows active projects, including unbound projects, with current
  revisions by default. Filter by project name or ID, or select **All history**
  to inspect every immutable project revision. Both the table and graph use the
  same filter; filtering does not change any project binding or delete history.
  Its Apache ECharts Sankey uses `nodeAlign: right`:
  baseline release -> project revision -> project. Link width counts revisions,
  not compliance, usage or source-code size. Selecting a revision opens its
  historical content without changing the project's current binding.

The version inventory also has a text table. Chart colors follow the Admin
theme, resize with the panel, and are disposed on sign-out. The pinned ECharts
6.0.0 asset loads on demand from jsDelivr with SHA-384 integrity verification.
Project data remains in the browser; it is not sent to the CDN. If loading fails,
the UI reports the error and retains the version table.
Labels truncate to fit narrow panels, while tooltips retain full node details.
Changing documents or projects prompts before discarding unstaged edits.
Saving a project revision requires staging edited change fields first; restoring
or collecting rules also protects local project drafts. Reload/close prompts
when drafts are present. Drafts remain browser-memory-only, not durable storage;
sign-out intentionally disposes the editor and chart.

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

This checkout records its approved adoption of ADC 1.1.23, project revision 2,
in `.adc/adc-lock.json`. The lock is the exported adoption snapshot, including
the preserved project overrides and document hashes; it is not a declaration
that subsequent local edits have been approved by the catalog. Keep the
exported baseline files intact, including the empty sample skill placeholder.
Future governance changes require administrator approval and a new project
revision through CGA, not edits to the retired standalone ADC repository.

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

### Central Collection of Project Rules

CGA / ADC is the rule authority. Project `.adc` files are working/distribution
copies, not a second release catalog. Use **Amendments > Preview all
project rules**, review each result, then **Collect available rule snapshots**
with an administrator's justification.

Collection reads only the registered checkout and includes `.adc/index.md`,
`bootstrap.md`, `prompt-rules.md`, root tool-rule pointers, and files under
`.adc/standards/`, `.adc/rules/` and `.adc/.github/`. It excludes the provenance
lock, credentials, Relay/edge-agent configuration, scratchpads, planning status
and knowledge context. It never executes a rule or imported workflow.

The preview identifies missing/unreadable roots explicitly. An unregistered
checkout must be registered first; an empty repository path must be corrected
in project settings. CGA does not guess another project's location by name.
Every active registered project with local rules must be `managed` before
claiming complete collection; `blocked` projects are not silently counted as
migrated. A visible checkout without `.adc` is reported as `no_local_rules`
only if it already has a centrally pinned baseline, not as a collected snapshot.

Each collection rechecks the preview's snapshot SHA256 and expected project
revision. Local edits or concurrent CGA updates return 409 without a committed
revision. Existing bindings retain their baseline; unbound projects explicitly
adopt the previewed baseline. Matching master rules are inherited; differences
become project overrides/amendments. Unrelated changes and central-only rules
are preserved. Collection is not an implicit baseline upgrade or deletion.

The new immutable revision and its `adc_rule_collections` receipt commit
atomically. Receipts record the registered source path, snapshot hash and
per-document SHA256 manifest; they are part of PostgreSQL backups. Published
master releases are never mutated. Original files are retained for offline
agents and source-control review, not deleted or rewritten during collection.
After collecting, make future rule changes in CGA and distribute approved
bundles using the existing preview-first Relay workflow.

Admin-only account endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/adc/versions` | Consistent release/project/revision inventory without document contents |
| `GET /api/adc/rules/preview` | Preview rule snapshots for every active registered checkout |
| `POST /api/adc/projects/{id}/rules/collect` | Collect the previewed snapshot using `release_id`, `expected_revision`, `snapshot_sha256`, and `reason` |

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