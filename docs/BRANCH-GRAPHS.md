# CGA-Relay Branch Graphs

CGA-Relay can route indexing and retrieval MCP calls to an isolated FalkorDB graph for a Git branch or other temporary ref. Existing callers that omit ref information continue to use the project's default graph.

## Graph Naming

The default graph keeps the existing behavior:

```text
project_name=ContextGraphAgent
ref_id=<omitted|main|master|default>
graph_name=contextgraphagent
```

A non-default ref uses a deterministic, FalkorDB-safe suffix:

```text
project_name=ContextGraphAgent
ref_id=feature/client-menu-order
graph_name=contextgraphagent__ref__feature_client_menu_order
```

Ref values are normalized to lowercase. Runs of characters outside `a-z` and `0-9` become one underscore, and leading or trailing underscores are removed. Raw ref names are never used directly as graph names.

## MCP Arguments

The following aliases are accepted by branch-aware Relay tools:

- Ref: `ref_id`, `branch`, or `git_branch`
- Parent ref: `parent_ref`, `base_ref`, or `base_branch`

`index_incremental` and `index_git_incremental` route jobs only to the derived ref graph. Their responses include `ref_id`, `parent_ref`, `graph_name`, and `parent_graph_name` when a ref argument is supplied. Omitting all ref arguments preserves the previous response shape and default graph routing.

The `sync` CLI command remains the machine scan and change-aggregation channel. It submits snapshot metadata for audit and aggregation but does not directly index FalkorDB graphs. Use the MCP indexing tools or their Relay CLI wrappers for branch graph indexing:

```powershell
cga-relay index git --config $HOME\.cga\relay.env --repo-path D:\Repos\ContextGraphAdmin --branch feature/client-menu-order --parent-ref main

cga-relay index incremental --config $HOME\.cga\relay.env --repo-path D:\Repos\ContextGraphAdmin --changed-path src\backend\main.py --ref feature/client-menu-order
```

The CLI accepts `--ref`, `--branch`, and `--git-branch` aliases. Parent aliases are `--parent-ref`, `--base-ref`, and `--base-branch`. Git indexing includes untracked files by default; pass `--no-include-untracked` to disable that behavior.

## Query Fallback

`query_impact_graph`, `get_optimized_context`, and `fetch_minimal_code` accept `ref_id` and optional `fallback_ref` arguments. A fallback is selected only when the requested non-default graph has no `File` nodes and the fallback graph has at least one `File` node.

Branch-aware responses include:

```json
{
  "ref_id": "feature/foo",
  "fallback_ref": "main",
  "requested_graph_name": "contextgraphagent__ref__feature_foo",
  "graph_name": "contextgraphagent",
  "fallback_graph_used": true
}
```

Read-cache keys include the active graph name so default, branch, and fallback results cannot collide.

## Promote After Merge

Call the Relay MCP tool `promote_ref` after the branch has been merged into the target working tree:

```json
{
  "ref_id": "feature/client-menu-order",
  "parent_ref": "main",
  "repo_path": "D:/Repos/ContextGraphAdmin",
  "delete_ref_graph": true
}
```

Promotion reads `File.path` values from the source ref graph, then queues those paths for incremental indexing from the current `repo_path` working tree into the target graph. It never copies raw nodes or edges between graphs. When `delete_ref_graph` is true, the source FalkorDB graph is deleted after the target indexing job is accepted.

The response includes `promoted_files`, `source_graph_name`, `target_graph_name`, `deleted_ref_graph`, and `index_result`.

The equivalent CLI command is:

```powershell
cga-relay refs promote --config $HOME\.cga\relay.env --repo-path D:\Repos\ContextGraphAdmin --ref feature/client-menu-order --parent-ref main --delete-ref-graph
```

## Current Limitations

- No full union or overlay query across default and branch graphs.
- No automatic Git merge detection.
- No CGA Admin UI branch graph page.
- No TTL or automatic cleanup for abandoned branch graphs.