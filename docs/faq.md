# CGA FAQ

## Is CGA just a knowledge graph?

CGA is related to a knowledge graph, but it is built for coding-agent execution rather than general knowledge management. It indexes source code as an AST-backed context graph with nodes and edges for files, symbols, imports, calls, definitions, containment, dependencies, and lightweight variable flow.

The goal is not to answer every question from a generic knowledge base. The goal is to help an AI coding agent retrieve the smallest useful subgraph before it reads raw source files or edits code.

For a deeper walkthrough, see [how-it-works.md](how-it-works.md).

## How is it different from embeddings or RAG?

Embedding retrieval is useful when semantic similarity is the best signal. CGA focuses on structural code relationships that embeddings often blur: which function calls which helper, which files import a module, which symbols live in a file, and which local variables flow into a return value.

In practice, the two approaches can complement each other. CGA gives the agent deterministic graph context first, then the agent can fall back to search, embeddings, or raw files when the graph does not provide enough evidence.

## Why can it reduce prompt tokens?

Coding agents often spend tokens re-reading whole files, repeated search results, or loosely related source blocks. CGA lets the agent ask for focused graph context such as a target symbol, neighboring relationships, dependency paths, imports, dependents, and variable flow.

The current live benchmark compares broad source context against graph-scoped CG context across 102 real-code cases. It reports a 90.44% average prompt-token reduction and a 13.34% average reduction in Hallucination Pressure Score. See [benchmarks/live-context-quality.md](benchmarks/live-context-quality.md) for the full table and caveats.

## Where are the benchmark results?

The current live benchmark summary is in [benchmarks/live-context-quality.md](benchmarks/live-context-quality.md). The benchmark guide and reproduction commands are in [benchmarks/README.md](benchmarks/README.md).

The headline result is the cross-project average across 102 real-code cases:

| Metric | Result |
|---|---:|
| Average baseline tokens | 5,474.95 |
| Average CG tokens | 483.29 |
| Average token reduction | 90.44% |
| Average baseline HPS | 17.66 |
| Average CG HPS | 13.94 |
| Average HPS reduction | 13.34% |

The report includes an important caveat: one project got a worse HPS under the conservative neighboring-context setup, while two improved. CGA should be evaluated by evidence coverage and task quality, not token reduction alone.

## Does CGA replace reading source files?

No. CGA is meant to make file reads more selective. The intended agent strategy is:

1. Query CGA for graph evidence.
2. Inspect the smallest relevant subgraph.
3. Open exact source excerpts only when line-level context is required.
4. Validate edits with the project test, lint, compile, or benchmark commands.

## Does CGA replace LSPs?

No. Language servers are excellent for precise language-aware operations such as go-to-definition, references, rename, diagnostics, and type information. CGA sits at a different layer: it persists repository relationships into a queryable graph, exposes agent-facing MCP tools, supports multi-project/team runtime views, and measures graph-scoped context quality.

A practical agent can use both: ask CGA for the relevant subgraph and dependency surface, then use an LSP when it needs precise language-server facts or refactoring support.

## How does CGA relate to AST-Grep?

AST-Grep is useful for structural search and rewriting. CGA is not mainly a pattern matcher. It indexes code into a persistent graph so agents can query relationships such as imports, calls, dependents, containment, architecture summaries, and variable flow across the repository.

The tools can complement each other: CGA can narrow the affected area, and AST-Grep can apply precise structural matches or transformations inside that area.

## What about tools like Serena, gitnexus, or codegraph?

CGA is in the same broad problem space: giving coding agents better context than plain text search. The emphasis here is a local-first runtime with a graph database, MCP-compatible retrieval tools, Admin Dashboard operations, deterministic context-quality benchmarks, and project evidence workflows.

The right comparison is empirical: run the same coding-agent tasks, measure evidence coverage, prompt tokens, fallback rate, validation results, and output quality.

## Can graph context hurt output quality?

Yes, if the graph slice is too narrow or misses required evidence. Token reduction is only useful when the agent still has enough evidence to make the right change.

CGA's intended strategy is conservative:

1. Query graph context first.
2. Check whether the graph hits are strong enough.
3. Fall back to raw files or search when graph evidence is insufficient.
4. Validate with project-specific checks.

The benchmark reports Hallucination Pressure Score in addition to token counts so the system can be judged on context risk, not compression alone.

## How does an agent use CGA?

CGA exposes MCP-compatible tools for symbol lookup, context retrieval, call graph queries, dependency analysis, import tracking, variable flow, and architecture summaries. The quickstart includes query clients and a CG-first strategy example. See [mcp-agent-query-quickstart.md](mcp-agent-query-quickstart.md).

## Can I reproduce the benchmark?

Yes. The benchmark guide includes commands for the live database-backed benchmark and sample deterministic benchmark runs. See [benchmarks/README.md](benchmarks/README.md).

Live reports can include real project identifiers, source excerpts, and host-specific paths, so generated live JSON and Markdown reports are intentionally ignored by git.

## Is CGA local-first?

Yes. CGA is designed around a local-first runtime for developer machines or team-controlled environments. It stores repository indexes and operational context in the configured CGA stack, with an Admin Dashboard for project registration, indexing, schedules, settings, and evidence views.