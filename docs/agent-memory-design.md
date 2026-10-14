# Agent Memory Layer: Borrowed Design Ideas

Status: Phases 1-3 implemented; the transparent proxy remains deferred.
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

### Phase 2: Derived layers and governance (implemented)
- Layered memory (idea 1): `workassist_get_memory_context` now returns the per-item fact layer (`memories`) plus a derived `scenarios` layer grouped by event type, with counts, latest timestamp, status counts, top tags and member IDs. Persona / team doctrine stays in `.adc/standards/`.
- Governance (idea 3): each memory item carries `governance` with `owner`, `visibility` (`project`), `review_status` (`unreviewed`) and a `version` derived from the content hash. No cross-project or team sharing path exists, so nothing leaves the authenticated project unreviewed.
- Full-text fallback and health flag (idea 6): `workassist_search_memory` ranks title, summary and tags with PostgreSQL full-text search (`websearch_to_tsquery` + `ts_rank`; this is not literal BM25). If full-text search fails, it falls back to an escaped substring match and returns `fts_available: false` with `search_mode: lexical_fallback`. Body text is never searched or returned.

### Phase 3: Skills and cold start (implemented)
- Skill library (idea 4): `backend.adc.skills` validates `SKILL.md` files: frontmatter `name` (kebab-case, matching the folder), `description`, SemVer `version`, and non-empty "When to Use", "Steps" and "Validation" sections. Run `python src/scripts/validate_skills.py .adc`. The two existing ADC sample skills are empty placeholders pinned by `.adc/adc-lock.json`; they are reported as invalid but were not modified.
- Cold start (idea 7): `python src/scripts/memory_cold_start.py --project-id <id> --root .` builds a dry-run plan from `docs/**/*.md` and `.adc/knowledge/**/*.md`. Each document contributes only its title, a first-paragraph summary capped at 400 characters (frontmatter, code, lists, tables and metadata lines skipped), and provenance (`path`, SHA-256). Documents with no summary, or whose summary looks like a credential, are skipped. `--apply` upserts with external ID `doc:<path>`, so reruns update instead of duplicating.

### Deferred
- Transparent proxy (idea 9).
- Numeric confidence scores, persisted review workflow, and team-level sharing. These need a product decision before implementation.

## Guardrails

- No new dependencies without authorization (see `.adc/prompt-rules.md`).
- Tests first for every phase.
- Credentials only via environment variables.
- All memory content is written in English.
- Any change to ContextGraph integration also updates `.adc/bootstrap.md` and the MCP wiring.
