# Context Quality Benchmarks

This directory contains deterministic ContextGraph context-quality benchmarks.

The benchmark compares baseline broad context against CG-reduced context for the same task. It reports token counts, useless tokens, gold evidence coverage, ambiguity, redundancy, and Hallucination Pressure Score (HPS).

Run the live database-backed project benchmark:

```powershell
python -m src.scripts.run_live_context_quality_benchmark `
  --projects BrowserAgent IcM_Automation ADC `
  --cases-per-project 34 `
  --output docs/benchmarks/context-quality-live-projects.report.json `
  --markdown docs/benchmarks/context-quality-live-projects.report.md `
  --run-date 2026-06-02
```

The live benchmark selects active projects from the CGA PostgreSQL `projects` table, reads indexed symbols from each FalkorDB project graph, and extracts context from real local repository files. The latest live report uses three projects with 34 deterministic symbol-level cases per project.

Generated live report files are local artifacts and are ignored by git because the full JSON can include project identifiers, source excerpts, and host-specific paths.

Run the sample CodexCLI and ClaudeCLI benchmark:

```powershell
python -m src.scripts.run_context_quality_benchmark `
  --input docs/benchmarks/context-quality.codex-claude.jsonl `
  --output docs/benchmarks/context-quality-report.json `
  --markdown docs/benchmarks/context-quality-report.md
```

Run the larger CodexCLI real test/source benchmark:

```powershell
python -m src.scripts.run_context_quality_benchmark `
  --input docs/benchmarks/context-quality.codexcli-real-snippets.jsonl `
  --output docs/benchmarks/context-quality.codexcli-real-snippets.report.json `
  --markdown docs/benchmarks/context-quality.codexcli-real-snippets.report.md `
  --repo-root <path-to-CodexCLI>
```

The CodexCLI real benchmark is generated from actual Rust test files and their paired implementation files. Baseline chunks use broad real files; CG chunks use deterministic source excerpts with `sourceFile` and `lineRange` metadata.

HPS is a pre-answer context risk score. Lower is better. It is deterministic and does not require an LLM call.

```text
HPS = 100 * (
    0.45 * missing_evidence_risk
  + 0.35 * noise_risk
  + 0.10 * redundancy_risk
  + 0.10 * ambiguity_risk
)
```

The benchmark should improve HPS by reducing noisy context while preserving gold evidence coverage.