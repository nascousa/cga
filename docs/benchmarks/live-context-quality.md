# Live Context Quality Benchmark

This report summarizes the current live database-backed CGA benchmark for ContextGraph retrieval quality.

The benchmark compares broad source context against graph-scoped CG context made from the target symbol excerpt plus one neighboring graph excerpt. It reports prompt-token reduction and Hallucination Pressure Score (HPS), a deterministic pre-answer context risk score.

This is a historical 2026-06-02 snapshot from the earlier fixed-neighbor methodology. It does not evaluate model answers, citation correctness, regression gates, or adaptive expansion. The current benchmark implementation freezes real `CALLS`, `IMPORTS`, and `FLOWS_TO` candidates and evaluates them separately with the controlled answer runner described in [README.md](README.md). The figures below have not been relabeled as answer-quality results.

Lower HPS is better.

```text
HPS = 100 * (
    0.45 * missing_evidence_risk
  + 0.35 * noise_risk
  + 0.10 * redundancy_risk
  + 0.10 * ambiguity_risk
)
```

## Methodology

The 2026-06-02 run used the currently running CGA runtime, selected three active projects from the PostgreSQL `projects` table, read indexed symbols from each FalkorDB project graph, and generated 34 deterministic symbol-level cases per project from real local source files.

Each case compares:

- Baseline broad source context.
- CG context with the target symbol excerpt and one neighboring graph excerpt.

The final results are averaged across 102 total real-code cases.

## Results By Project

| Project | Cases | Baseline HPS | CG HPS | HPS Reduction | Baseline Tokens | CG Tokens | Token Reduction |
|---|---:|---:|---:|---:|---:|---:|---:|
| ADC | 34 | 13.91 | 14.35 | -15.64% | 2,831.74 | 313.00 | 88.88% |
| BrowserAgent | 34 | 19.24 | 12.37 | 34.46% | 7,471.56 | 120.56 | 98.31% |
| IcM_Automation | 34 | 19.83 | 15.08 | 21.20% | 6,121.56 | 1,016.32 | 84.12% |
| **Average** | **102** | **17.66** | **13.94** | **13.34%** | **5,474.95** | **483.29** | **90.44%** |

## Cross-Project Average

| Metric | Result |
|---|---:|
| Projects | 3 |
| Cases per project | 34 |
| Total real-code cases | 102 |
| Average baseline HPS | 17.66 |
| Average CG HPS | 13.94 |
| Average HPS reduction | 13.34% |
| Average baseline tokens | 5,474.95 |
| Average CG tokens | 483.29 |
| Average token reduction | 90.44% |

The live run is intentionally not flattened into a single success claim: ADC's HPS increased slightly under this conservative neighboring-context setup, while BrowserAgent and IcM_Automation improved. Across all 102 real-data cases, CG reduced average tokens by 90.44% and reduced average HPS by 13.34%.

## Run The Current Live Benchmark

Run the current target-first database-backed benchmark and freeze its graph expansion candidates. Because the methodology has changed since this historical snapshot, this command produces a new case-set hash and should not be expected to reproduce the figures above:

```powershell
python -m src.scripts.run_live_context_quality_benchmark `
  --projects BrowserAgent IcM_Automation ContextGraphAgent `
  --cases-per-project 34 `
  --frozen-cases docs/benchmarks/context-quality-live-projects.cases.jsonl `
  --output docs/benchmarks/context-quality-live-projects.report.json `
  --markdown docs/benchmarks/context-quality-live-projects.report.md `
  --run-date 2026-06-02
```

Live JSON and Markdown reports are generated locally and ignored by git because they may include real project identifiers, source excerpts, and host-specific paths.

For the deterministic benchmark model and sample benchmark commands, see [README.md](README.md).