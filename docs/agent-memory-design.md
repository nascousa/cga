# Agent Memory Layer: Borrowed Design Ideas

Status: Phase 1 implemented locally; phases 2-3 remain proposed.
Reference studied: TencentDB Agent Memory (`TencentCloud/tencentdb-agent-memory`, v2.0.3-beta.1).
Licensing note: the upstream license metadata was `NOASSERTION` when reviewed. Only architectural ideas are adopted here; no upstream code, schemas, or prompts are copied.

## Positioning

CGA stays the system of record for facts and code evidence (ContextGraph, Relay, Work Briefing).
A memory layer is an optional, derived view on top of those records. It must never become a second source of truth, and it must never hold raw source code or secrets.

## Ideas Worth Keeping

| # | Idea | CGA mapping | Phase |
|---|------|-------------|-------|
| 1 | Layered memory: raw records -> atomic facts -> scenario summaries -> persona / team doctrine | Work Briefing activities are the raw layer. Add derived fact and scenario summaries per project; team doctrine maps to `.adc/` standards. | 2 |
| 2 | Inject only high-level context; expose details through read-only query tools | Keep prompt injection to a short project/persona summary. Details stay behind `workassist_*`, `query_impact_graph`, `get_optimized_context`, and `fetch_minimal_code`. Preserves prompt size and KV cache reuse. | 1 |
| 3 | Asset governance fields: owner, visibility (`private` / `team` / `restricted`), version, status, review-before-share | Add governance metadata to derived memory items. Sharing to `team` requires an explicit review state. | 2 |
| 4 | Versioned skill library with trigger boundaries, steps, and validation rules | Align with `.adc/` skills: each skill declares when it applies, ordered steps, and how success is verified; versions are pinned in `.adc/adc-lock.json`. | 3 |
| 5 | Separate Memory (what happened, preferences) from Knowledge (CodeGraph, wiki) | ContextGraph remains Knowledge. The memory layer only stores activity-derived summaries and references into the graph. | 1 |
| 6 | Lexical (BM25) fallback with a health flag for full-text availability | When vector/graph retrieval is degraded, fall back to PostgreSQL full-text search and report `fts_available` in health output. | 2 |
| 7 | Cold-start import of existing docs and sessions | One-time backfill from `docs/`, `.adc/knowledge/`, and historical Work Briefing records into derived summaries. | 3 |
| 8 | Provenance on every memory item | Each item stores source URL, project ID, commit SHA, source activity ID, and a confidence score. Raw bodies are not stored. | 1 |
| 9 | Transparent proxy that injects context and writes conversations back | Long-term option only. Not adopted until phases 1-3 prove value. | Deferred |

## Not Adopted

- A separate memory database or service stack. CGA reuses its existing PostgreSQL and Redis.
- Automatic write-back of full conversations. Only sanitized summaries are persisted.
- Any upstream code, prompts, or data formats.

## Phased Adoption

### Phase 1: Read-only summary export
- Added the read-only `workassist_get_memory_context` MCP tool, scoped to the authenticated project and capped at 100 items.
- Each returned item contains only its summary (falling back to title), event type, status, tags, and provenance. It omits `body_text`, raw metadata, and embedding text, following the safe projection pattern in `src/backend/ai_first/service.py`.
- Provenance includes the activity ID, source item ID, project ID, occurred-at timestamp, and a sanitized source URL with credentials, query, and fragment removed. A commit SHA is extracted when the URL has a recognizable commit path.
- `confidence` is `null` until CGA has a defensible confidence-scoring method; the implementation does not invent a score.
- This is read-only and derives results from existing Work Briefing records; no memory database, persisted summary, or conversation write-back was added.
- Unit tests cover authenticated project scoping, sanitized provenance, and exclusion of body, metadata, embedding text, and URL credentials.
- ContextGraph indexing and change aggregation were unavailable during this implementation; local tests are not a substitute for that workflow.

### Phase 2: Derived layers and governance
- Add atomic-fact and scenario summary layers (idea 1).
- Add governance fields and a review state for team sharing (idea 3).
- Add the full-text fallback and health flag (idea 6).

### Phase 3: Skills and cold start
- Formalize skill metadata (idea 4).
- Run a one-time backfill (idea 7).

## Guardrails

- No new dependencies without authorization (see `.adc/prompt-rules.md`).
- Tests first for every phase.
- Credentials only via environment variables.
- All memory content is written in English.
- Any change to ContextGraph integration also updates `.adc/bootstrap.md` and the MCP wiring.
