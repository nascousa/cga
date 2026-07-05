# How CGA Works

CGA is a graph-first context service for AI coding agents. It is designed for the moment before an agent reads code, writes a patch, or explains a system: the moment when the agent needs to know which files and symbols actually matter.

## The Problem

Most coding agents can search, open files, and ask for more context. That works, but it often burns tokens on whole files, duplicate snippets, loosely related search hits, and repeated context loading. The agent may still miss the one definition, caller, import, or data-flow hop that controls the task.

CGA addresses that by making repository structure queryable before raw source is loaded into the prompt.

## The Context Graph

CGA indexes source code into a graph of repository facts:

- Files and directories.
- Symbols such as functions, classes, methods, and declarations.
- Containment relationships such as file-to-symbol and class-to-method.
- Imports and file-level dependencies.
- Calls and symbol-to-symbol relationships.
- Lightweight variable flow for supported languages.
- Project metadata and operational evidence used by the local runtime.

The graph is stored in the configured CGA runtime and exposed through MCP-compatible tools.

## Agent Flow

The intended agent workflow is CG-first:

1. Query CGA for the task, target symbol, file, dependency path, or architecture area.
2. Review the returned graph context and evidence-bearing excerpts.
3. Open raw source only for the few files or line ranges that need exact inspection.
4. Make the edit.
5. Validate with the repository's tests, lint, type checks, benchmark, or runtime checks.

For example, instead of asking an agent to read every file that mentions `InvoiceService`, the agent can ask for the symbol, callers, imports, dependents, and nearby graph relationships first. That gives it a smaller map of the affected area before it spends tokens on source text.

## Why This Is Different From Broad Search

Search finds text matches. CGA returns relationships. A text match can tell the agent where a name appears; graph context can tell it which symbol owns the behavior, which file imports it, which methods call it, and which dependency path connects two modules.

That distinction matters when the task is not just "find this string" but "change this behavior without missing callers, dependencies, or related evidence."

## Why This Is Different From Embedding-Only RAG

Embeddings are useful for semantic similarity, but code changes often depend on structural facts. A semantically similar chunk may not be the caller, definition, import owner, or data-flow source needed for a safe edit.

CGA treats structural evidence as the first retrieval route. Embeddings, search, and raw file reads can still be used as fallbacks when graph evidence is incomplete.

## Where LSPs Fit

CGA does not replace language servers. LSPs are strong at precise language-aware operations: definitions, references, diagnostics, type information, rename, and editor refactoring.

CGA focuses on persistent graph retrieval and agent workflow: multi-file relationship queries, MCP-facing context tools, runtime indexing, project operations, and context-quality measurement. A strong coding-agent setup can use CGA for graph-scoped retrieval and an LSP for precise language-server facts.

## Where AST-Grep Fits

AST-Grep is useful for structural search and rewrite rules. CGA is a persistent relationship graph, not primarily a pattern-matching engine.

The two can work together: CGA narrows the relevant area of the repository, then AST-Grep can search or transform syntax patterns inside that smaller area.

## Quality Controls

Reducing tokens is not enough. A smaller prompt that omits required evidence can make output worse.

CGA's benchmark model compares broad source context with graph-scoped context and reports both token reduction and Hallucination Pressure Score. HPS is a deterministic pre-answer context risk score that considers missing evidence risk, noise risk, redundancy risk, and ambiguity risk.

The current live benchmark reports a 90.44% average token reduction and a 13.34% average HPS reduction across 102 real-code cases. The report also preserves a caveat: one project got worse under the conservative neighboring-context setup while two improved.

See [benchmarks/live-context-quality.md](benchmarks/live-context-quality.md) for the current report and [benchmarks/README.md](benchmarks/README.md) for reproduction commands.

## Failure Modes

CGA should fall back when graph evidence is weak. Common cases include:

- A repository was not indexed recently enough.
- A language parser does not capture a required relationship.
- The task depends on runtime behavior, configuration, generated code, or external services.
- A narrow graph slice does not include enough neighboring evidence.

The agent should treat graph context as the first map, not as the only source of truth. Exact source inspection and executable validation still matter.