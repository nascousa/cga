# CGA-Relay Branch Graphs

CGA-Relay can route indexing and retrieval MCP calls to an isolated FalkorDB graph for a Git branch or other temporary ref. Existing callers that omit ref information continue to use the project's default graph.

## Graph Naming

The default graph keeps the existing behavior:

```text
project_name=ContextGraphAgent
ref_id=<omitted|main|master|default>
graph_name=contextgraphagent
```

A non-default ref uses a reserved, versioned namespace. Both hashes are full
SHA-256 hexadecimal digests of UTF-8 strings:

```text
project_name=ContextGraphAgent
project_id=<registered immutable external project ID>
ref_id=feature/client-menu-order
graph_name=__cga_ref_v2__<SHA-256(project_id)>__<SHA-256(ref_id)>
```

The angle-bracket values above are placeholders, not literal graph names. Ref
values have surrounding whitespace removed but otherwise retain their exact
case and punctuation. `feature/a-b`, `feature/a_b`, `feature/a/b` and
`Feature/a-b` therefore have different graphs. Default aliases are exactly
`main`, `master`, `default`, or the omitted/empty value; `Main` is a distinct ref.
Branch graph names remain stable if the project's display name changes.

`__cga_` is reserved for server-owned graphs, including branch and staging
graphs. Project creation/update and server-side project binding reject this
namespace. Passing a physical graph name as an MCP `project_name` cannot switch
away from the authenticated project/ref. Relay derives branch names from the
authenticated DB project ID, not from a caller-supplied project ID or graph name.
Existing project main graph names, including names containing `__ref__`, are
not renamed or deleted by this change.

## Repository Authorization

MCP project tokens and account-authenticated Relay calls bind the project's
registered `projects.repo_path` from the database. A requested `repo_path` selects
that root using purely lexical comparison against registered/canonical aliases;
the requested path is never probed to discover whether an unrelated directory
exists. Naming a readable checkout or supplying only apparently safe
`changed_paths` does not grant access to it.

- If `repo_path` is empty in the DB, the server's configured repository search
  roots/default roots are used to discover the registered project checkout.
  Ambiguous matching directories fail closed; set an explicit DB `repo_path`.
- Existing Windows-to-container mapping is supported, for example a registered
  `D:\Repos\Example` checkout mounted as `/repos/Example`.
- Aliases are derived from the registration and trusted mount configuration,
  not a request's `Repos` suffix or basename. An explicit Windows registration
  fixes the allowed drive. For a native `/repos/Example` registration, the
  historical host alias is `D:\Repos\Example`; configure `CGA_HOST_REPOS_ROOT`
  when the host mount differs. Windows aliases are case-insensitive; POSIX
  aliases remain case-sensitive.
- Index entry points, Git discovery, queue submission, changed files, deletion
  paths, source snippets and server-side keyword fallback all enforce the
  project boundary. Absolute paths and symlink targets outside the checkout are
  rejected. Files must pass a lexical boundary check before symlink resolution
  and a physical boundary check afterward. Missing files are allowed only as
  in-root tombstones.
- An absent authenticated scope or unregistered root does not fall back to the
  API's current working directory or a process-wide repository root.
- Existing administrator index routes still enforce their administrator
  dependency and receive a DB-bound project context. Trusted internal callers
  must explicitly use `bind_project_scope(registered_project_scope(db_record))`;
  passing a project name alone is not authorization.

Workers share `backend.indexer.paths.resolve_repo_root(repo_path) -> Path` and
`resolve_changed_path(repo_path, resolved_root, changed_path) -> str`. The latter
returns a canonical **absolute** in-root filename, including missing files.
These helpers establish filesystem boundaries; they do not replace the
request-layer database authorization.
`normalize_repo_path(path) -> str` supports directory and file-path
normalization, including missing Windows-to-container tombstones. It performs
no filesystem I/O and does not establish project ownership. Only the registered
authority is resolved as a root. Network/device paths are rejected before
filesystem probing.
Workers can additionally call
`await backend.auth.access.authorized_job_repo_root(repo_path, graph_name)`
to revalidate queued work against current active DB registrations without an
HTTP context. Unknown/legacy branch owners and changed roots fail closed.

## MCP Arguments

The following aliases are accepted by branch-aware Relay tools:

- Ref: `ref_id`, `branch`, or `git_branch`
- Parent ref: `parent_ref`, `base_ref`, or `base_branch`

`index_full`, `index_incremental` and `index_git_incremental` route jobs only to
the derived ref graph. Their responses include `ref_id`, `parent_ref`,
`graph_name`, and `parent_graph_name` when a ref argument is supplied. Omitting
all ref arguments preserves the default graph routing. The bridge's
`index_full` tool is also available through the project/account Relay HTTP
tool-call endpoints for complete branch rebuilds; it does not require a raw
physical graph override.

The `sync` CLI command remains the machine scan and change-aggregation channel. It durably stores validated snapshot contents and tombstone paths as project-scoped replayable batches, but does not directly index FalkorDB graphs. Use the MCP indexing tools or their Relay CLI wrappers for branch graph indexing:

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
  "requested_graph_name": "__cga_ref_v2__<project-id-hash>__<exact-ref-hash>",
  "graph_name": "contextgraphagent",
  "fallback_graph_used": true
}
```

Read-cache keys include the active graph name, authenticated project ID,
registered root, authorization version and published graph generation.
An atomic graph commit changes the generation and therefore the read-cache
key; correctness does not depend on a successful cache invalidation. If a
published generation is unavailable, the server bypasses its read cache.
Ambiguous legacy branch graphs block the request **before** fallback; an empty
new graph is not an excuse to silently read a collision-prone legacy graph.

## Legacy Branch Migration (Administrator Required)

The old `<project>__ref__<normalized-ref>` format can identify either another
project's main graph or several distinct refs. Its name and `File.path` nodes
are not proof of ownership. The server never automatically adopts, renames,
copies or deletes one of these legacy graphs.

When an old physical name exists for a requested ref, reads, indexing and
promotion return **409** until an administrator explicitly acknowledges that
exact project/ref migration. The default project graph is not silently used
instead, even when `fallback_ref` was supplied.

Migration procedure:

1. Pause affected ref traffic and indexing, including any project whose main
   graph collides with the legacy name. Take and verify recoverable graph and
   project-registration backups before changing anything.
2. Investigate ownership using project registrations, known checkouts/commits
   and operator history. Do not infer it solely from graph contents. If it
   cannot be proved, preserve the old graph as untrusted historical data and
   rebuild from the correct, administrator-approved checkout. Never overwrite
   a colliding project's main graph.
3. After backup and review, an administrator with direct datastore access may
   write this exact JSON acknowledgement at
   `cga:ref-migration:v2:<new-physical-graph-name>`:

   ```json
   {
     "version": 2,
     "project_id": "<registered external project ID>",
     "ref_id": "feature/foo",
     "legacy_graph_name": "contextgraphagent__ref__feature_foo"
   }
   ```

   The marker attests to administrator-reviewed retirement of the legacy
   mapping; it does **not** claim ownership of its data. Each exact ref needs
   its own marker, even if several old refs normalized to the same name. No
   MCP or Relay request can write this marker.
4. While traffic remains paused, rebuild the new ref graph using the approved
   registered checkout and a Relay tool-call payload such as:

   ```json
   {
     "tool": "index_full",
     "arguments": {
       "repo_path": "D:/Repos/ContextGraphAgent",
       "ref_id": "feature/foo"
     }
   }
   ```

   Submit to `/api/project/cga-relay/mcp-tool` with the matching project token,
   or the account-authenticated Relay tool-call endpoint with project access.
   Check the returned job until it is `done` with empty `errors`, verify
   representative queries, and only then resume traffic. Queued acceptance is
   not migration success.
5. Retain the verified backups and legacy graph until an administrator applies
   a separate retention decision. Back up migration markers alongside project
   registrations and graph data. This release does not implement a lossless
   legacy graph rename or automatic data migration.

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

Promotion queues a **full, atomic rebuild** of the target graph from the
registered checkout's current merged working tree. It does not enumerate
source `File.path` nodes: a deleted file may have no surviving node, so using
those nodes would lose deletion/tombstone information. It never copies raw
nodes or edges between graphs. The source and target must be different refs.

The call waits up to 120 seconds for the target job. `delete_ref_graph=true`
deletes the source **only after** the target reports `done`, matches the
expected target graph, and reports empty indexing `errors` and a positive
indexed-file count. The server additionally verifies that the target exists,
its publication generation changed and equals the job's `published_generation`
receipt, and it contains files. Queued,
processing, failed, missing, timed-out, cancelled or unverifiable results
preserve the source. A timed-out queued job may still complete later; inspect
its status and retry deliberately rather than treating a timeout as success.
An empty-scan protection failure returns `failed`; an empty rebuild or
unpublished/empty target returns `noop`. Neither permits source deletion.

The source generation is captured before submitting the target job. Source
deletion holds the source write lease and atomically compares both the captured
source generation and the target's job receipt in Redis before unlinking the
source. Another target publication cannot interleave with that comparison and
deletion. A source or target replacement retains the source; if it occurs after
initial verification, `deleted_ref_graph` is `false` and `reason` is
`promotion_graph_changed_source_retained`. A replacement observed during initial
verification returns `noop`. Review newer changes before another promotion.
Legacy job results without a generation receipt never authorize deletion.

The response includes `status` (`done`, `pending`, `noop` or `failed`), `reason`, `rebuild_mode=full`,
`source_graph_name`, `target_graph_name`, `deleted_ref_graph`, `submitted_job`
and `index_result`. There is no `promoted_files` list because the complete
merged checkout, including removals, is authoritative.

The equivalent CLI command is:

```powershell
cga-relay refs promote --config $HOME\.cga\relay.env --repo-path D:\Repos\ContextGraphAdmin --ref feature/client-menu-order --parent-ref main --delete-ref-graph
```

## Current Limitations

- No full union or overlay query across default and branch graphs.
- No automatic Git merge detection.
- No CGA Admin UI branch graph page.
- No TTL or automatic cleanup for abandoned branch graphs.