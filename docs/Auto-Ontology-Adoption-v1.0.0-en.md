# Auto Ontology Adoption for CGA

- Document version: 1.0.0
- Language: en
- Research date: 2026-11-09
- Upstream: https://github.com/NVIDIA/auto-ontology
- Reviewed revision: `c447003f72a822301e927196a4ecb5b597c308d5`

## Conclusion

Adopt the **semantic-layer-first design, controlled tool surface, deterministic
queries, and traceable evidence**, rather than embedding the complete Auto
Ontology application in the CGA backend.
This delivery is an independent implementation informed by research. It does not
call NVIDIA services, integrate the upstream SDK, or automatically generate a
business ontology. CGA retains its local-first architecture, AST graph, and
existing authorization boundaries.

## Research Evidence

All source links are pinned to the reviewed revision so later changes to the
upstream main branch are not presented as evidence for this adoption.

| Source | Verified findings | Implications for CGA |
| --- | --- | --- |
| [README](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/README.md) | Enterprise data catalog, business semantic layer, and natural-language-to-SQL; FastAPI, Next.js, Postgres/pgvector; default NIM model endpoints | A context layer for data analytics, not a replacement for code AST indexing |
| [MCP documentation](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/docs/mcp.md) | Discover business terms, check answerability, then ask questions; reuse user authorization; read-only tools; handshake instructions | Tools should explain meaning and call order rather than expose arbitrary low-level queries |
| [Tool implementation](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/mcp/auto_ontology_mcp/tools.py) | Explicit allowlist, user-oriented names and descriptions, and contract-drift checks | Add two bounded tools to CGA without creating an arbitrary Cypher executor |
| [Readiness checks](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/mcp/auto_ontology_mcp/readiness.py) | Compilation status, connectivity, and catalog state are not interchangeable; errors and authorization failures must remain distinct | An available CGA contract does not prove that a graph is indexed, current, or answerable |
| [Dependency manifest](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/pyproject.toml) | Python 3.12-3.13, LangChain/LangGraph, multiple database connectors, and model SDKs | Direct embedding would substantially expand dependencies and operations, unnecessarily at this stage |
| [License](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/LICENSE) | Apache-2.0 | Adoption is permitted subject to the license; future distribution of upstream code must retain applicable notices |

Upstream results for business-data querying or text-to-SQL cannot be treated as
results for CGA code retrieval. This delivery makes no new claim of token savings,
accuracy improvement, or latency reduction.

## Implemented Design

### 1. Discoverable Code-Graph Semantic Contract

The new [ontology.py](../src/backend/graph/ontology.py) defines contract version
`1.0.0`.

| Entity | Exact identity |
| --- | --- |
| Repository | path |
| File | path |
| Symbol | qualified_name |
| Variable | qualified_name |

| Relation | Direction | Semantic boundary |
| --- | --- | --- |
| CONTAINS | Repository -> File | Not arbitrary directory or class containment |
| DEFINES | File -> Symbol | Does not imply additional class-to-method edges |
| IMPORTS | File -> File | Resolved local imports, not an external dependency catalog |
| CALLS | Symbol -> Symbol | Static call resolution; does not guarantee runtime execution or exact overload resolution |
| USES_VARIABLE | Symbol -> Variable | Scoped variables, subject to parser support |
| FLOWS_TO | Variable -> Variable | Lightweight data flow, potentially including resolved cross-function argument/return flows; not complete taint analysis |

The contract exposes only the six relations supported by current graph writes.
The schema header was corrected to remove the unwritten `REFERENCES` relation
and include variable relations. No graph migration was introduced.

### 2. Deterministic, Bounded One-Hop Queries

The [MCP server](../src/backend/tools/server.py) adds:

- `get_graph_ontology()`: returns entities, identities, relations, limitations,
  and the recommended workflow.
- `query_graph_relation(relation, identity, direction, limit)`: accepts only the
  six relations, matches exact identities, supports inbound and outbound
  traversal, and limits results to 1-100 items.

Only contract-owned labels, properties, and relation names enter the query
structure; identities and limits are parameterized. One extra item is fetched
to determine `has_more`, so reaching the limit alone does not falsely imply
truncation. Results include canonical source/target direction, indexed file and
line locations, available data-flow edge properties, graph name, contract
version, and the actual Cypher and parameters.

Empty results use `no_matches`, which explicitly does not prove a relation is
absent. Graph connection and execution failures continue to raise errors rather
than becoming successful empty results. No SQL is generated or executed, and no
arbitrary Cypher input is accepted.

### 3. Existing Project Authorization, Branch Routing, and Caching

- Reuse existing project/ref authentication context and graph routing.
- Reuse the read cache isolated by project and graph generation, plus existing
  trace recording.
- Add no shared service account and bypass no communication configuration or
  authorization.
- Both the [Relay bridge](../src/backend/cga_relay/router.py) and
  [Rust Relay](../src/cga-relay/src/main.rs) support the two tools. Their new
  schemas specify allowed relations, directions, and limit ranges.
- Backend MCP handshake instructions describe the semantic workflow. Relay tool
  descriptions explain call order and limitations as well.
- Existing indexing, retrieval, administration UI, and graph database structure
  remain unchanged in this adoption snapshot.

### 4. Usage

1. Call `get_graph_ontology` to understand the current semantics.
2. Obtain an exact identity through `find_symbol` or an existing search result.
3. For example, query callers using `CALLS`, that symbol identity, and `inbound`.
4. Inspect provenance, scope, and `has_more`; read source and run tests when
   necessary.

See the [MCP quickstart](mcp-agent-query-quickstart.md#typed-semantic-graph-queries)
for parameters. A default, minimal result does not replace complete change-impact
analysis or validation against live source.

## Validation

Local validation used the existing virtual environment and running FalkorDB.
Tests created and cleaned up only randomly named test graphs, without modifying
existing project graphs.

- Targeted Python regressions cover the contract, both directions of every
  relation, invalid parameters, injection strings, result boundaries, MCP
  discovery and calls, unauthorized access rejection, branch routing, and
  existing MCP and Relay behavior.
- Real-graph tests index two Python files through the existing `IndexPipeline`,
  verify inbound/outbound results and provenance for all six relations, and run
  existing MCP end-to-end cases.
- Rust tests verify tool-discovery schemas and authenticated forwarding, along
  with related MCP regressions.
- Static checks include mypy for the new module and Ruff for modified Python
  files.

Recorded results:

| Validation | Result |
| --- | --- |
| Targeted Python regressions, using the pytest command below | 107 passed, including real-graph tests, with no skips |
| Targeted Rust `mcp_` regressions | 15 passed, including actual authenticated forwarding for both new tools |
| Rust `ontology_tools_are_discoverable_with_typed_arguments` | 1 passed |
| mypy contract compiler | passed |
| Ruff, retaining the verified pre-existing E402 exception | passed |
| `git diff --check` | passed |

The first real-graph connection failed because default port `16379` was closed;
tests passed after using the existing local port `26381`. Cargo was not on PATH,
so validation used the installed `%USERPROFILE%\.cargo\bin\cargo.exe` without
installing dependencies. Existing `server.py` E402 findings arise from deferred
ADC imports and were verified against HEAD; unrelated initialization order was
not changed.

Reproduction commands (PowerShell, repository root):

```powershell
$env:FALKORDB_PORT = "26381"
.\.venv\Scripts\python.exe -m pytest src\tests\test_graph_ontology.py src\tests\test_mcp_tools.py src\tests\test_mcp_e2e.py src\tests\test_cga_relay_router.py --no-cov -ra
.\.venv\Scripts\python.exe -m mypy --follow-imports=skip src\backend\graph\ontology.py
.\.venv\Scripts\python.exe -m ruff check --ignore E402 src\backend\graph\ontology.py src\backend\tools\server.py src\backend\cga_relay\router.py src\tests\test_graph_ontology.py src\tests\test_cga_relay_router.py
Push-Location src\cga-relay
try {
    & "$env:USERPROFILE\.cargo\bin\cargo.exe" test --locked mcp_
    & "$env:USERPROFILE\.cargo\bin\cargo.exe" test --locked ontology_tools_are_discoverable_with_typed_arguments
} finally {
    Pop-Location
}
```

Cargo commands run from the crate directory so its `.cargo/config.toml` is loaded.

## Exclusions and Conditions for Future Work

- **No Auto Ontology service was installed or deployed**: no NIM calls, API keys,
  or source-code transfer.
- **No upstream code or model weights were imported**: this is an original
  implementation with source attribution, without new third-party runtime
  dependencies. Copying or distributing upstream components later requires
  updating third-party notices and reviewing dependency licenses.
- **No automatic business-term generation, term certification, corrective
  write-back, or natural-language compiler was introduced**: these change
  persistence and governance and require separate design, approval, and quality
  evaluation.
- **No new aggregate readiness decision was implemented**: contract availability,
  index existence, and freshness remain distinct. Existing index status, actual
  queries, and provenance checks are still required.
- **At the time of this adoption snapshot, no deployment, commit, or pull request
  had been created**: other tasks had pending changes in the worktree, so this
  stage completed only local implementation and validation without releasing
  those unrelated changes.
- If CGA later needs to answer business-data questions, prefer a separately
  deployed Auto Ontology instance as a peer MCP service in the same agent harness,
  rather than embedding its database connectors in CGA. Before rollout, verify
  authorization, outbound-data policies, communication security, deployment
  readiness, and pinned versions.
- Future outcome evaluation should use real CGA code tasks to compare tool
  misselection, evidence coverage, tokens, latency, and final task correctness.
  These tests demonstrate only the behavioral contract and compatibility.
