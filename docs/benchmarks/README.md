# Context Quality Benchmarks

This directory contains deterministic ContextGraph context-quality and controlled answer-quality benchmarks.

For the current live multi-project benchmark summary, full result table, caveats, and reproduction command, see [live-context-quality.md](live-context-quality.md).

The pre-answer benchmark compares broad baseline context against target-first CG context for the same task. It reports token counts, useless tokens, gold evidence coverage, ambiguity, redundancy, and Hallucination Pressure Score (HPS). HPS is a retrieval-risk proxy, not a model-answer score.

Run the live database-backed project benchmark:

```powershell
python -m src.scripts.run_live_context_quality_benchmark `
  --projects BrowserAgent IcM_Automation ContextGraphAgent `
  --cases-per-project 34 `
  --frozen-cases docs/benchmarks/context-quality-live-projects.cases.jsonl `
  --output docs/benchmarks/context-quality-live-projects.report.json `
  --markdown docs/benchmarks/context-quality-live-projects.report.md `
  --run-date 2026-06-02
```

The live benchmark selects active projects from the CGA PostgreSQL `projects` table, reads indexed symbols from each FalkorDB project graph, and extracts context from real local repository files. Each frozen case stores the target excerpt plus a deterministic, bounded pool of actual `CALLS`, `IMPORTS`, and `FLOWS_TO` candidates. Existing frozen manifests are immutable unless `--refresh-frozen-cases` is explicitly supplied.

Generated live report files are local artifacts and are ignored by git because the full JSON can include project identifiers, source excerpts, and host-specific paths.

## Controlled Answer Evaluation

Set an API key for an OpenAI-compatible chat-completions endpoint, then run paired baseline and CG answers over the same frozen cases:

```powershell
$env:CGA_EVAL_API_KEY = "<api-key>"
python -m src.scripts.run_answer_quality_benchmark `
  --input docs/benchmarks/context-quality-live-projects.cases.jsonl `
  --model <model-deployment> `
  --base-url <openai-compatible-base-url> `
  --max-expansion-depth 2 `
  --max-expansion-chunks 6 `
  --max-cg-context-tokens 4000 `
  --requests-per-minute 10 `
  --checkpoint tmp/benchmarks/answer-quality.checkpoint.json `
  --output docs/benchmarks/answer-quality.report.json `
  --markdown docs/benchmarks/answer-quality.report.md `
  --fail-on-regression
```

The runner fixes the model, prompt template, temperature, seed, and zero-tool budget across baseline and CG calls. It requires structured answers with evidence-ID citations, scores required facts and citations deterministically, and applies task-pass and citation-coverage gates per project. Natural-language fact checks use the symbol name, source filename, relation verb, and local target name. Exact repository paths and qualified relation IDs remain in the prompt and must pass the separate grounded citation gate; internal IDs are not required as prose.

The initial CG runs calibrate HPS against actual pass/fail outcomes. Missing evidence, answer failure, or HPS above the calibrated threshold can trigger expansion. Expansion only consumes the frozen graph candidate pool, adds one graph depth at a time within the configured chunk and token budgets, reruns the same model controls, and records every attempt and stop reason. The report preserves both the initial and final CG outcomes and classifies failures using retrieval coverage and baseline behavior.

### GitHub Models with resumable free-tier runs

An existing GitHub CLI login can provide a process-only credential without permanently configuring CGA model environment variables or running a local model endpoint. Choose `--requests-per-minute` for the model and account entitlement; `10` below is an illustrative conservative value, not a universal quota.

```powershell
$env:CGA_EVAL_API_KEY = gh auth token
try {
  python -m src.scripts.run_answer_quality_benchmark `
    --input tmp/benchmarks/context-quality-adaptive-102.cases.jsonl `
    --model openai/gpt-4.1-mini `
    --base-url https://models.github.ai/inference `
    --requests-per-minute 10 `
    --max-retries 4 `
    --retry-backoff-seconds 2 `
    --max-retry-delay-seconds 60 `
    --max-expansion-depth 2 `
    --max-expansion-chunks 6 `
    --max-cg-context-tokens 4000 `
    --checkpoint tmp/benchmarks/answer-quality.github-models.checkpoint.json `
    --output tmp/benchmarks/answer-quality.github-models.json `
    --markdown tmp/benchmarks/answer-quality.github-models.md
} finally {
  Remove-Item Env:CGA_EVAL_API_KEY -ErrorAction SilentlyContinue
}
```

The runner retries transient `408`, `429`, and selected `5xx` responses, honors numeric and HTTP-date `Retry-After` values, and checkpoints each completed initial or adaptive case using atomic file replacement. If a requested server delay exceeds `--max-retry-delay-seconds`, the run stops instead of retrying early. After the quota window resets, rerun the identical command with `--resume`. The checkpoint is accepted only when its frozen-case SHA, model configuration, regression gates, and expansion budgets match; completed model calls are not repeated.

No answer-level model result is claimed by this documentation. A report is evidence only after the command completes against a named frozen-case hash and model configuration.

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