# Graph Integrity And Recovery

## Generation-based indexing

Starting with 1.30.125, indexing builds a private copy of the project's graph.
A renewable, per-graph Redis lease serializes writers. Only a completed build
can atomically replace the published graph; a lost lease, parse error, database
error, or source edit during parsing prevents publication. Readers continue to
see the last successful generation. Temporary graphs expire after an abandoned
build and are not project or branch names.

The graph database must support `GRAPH.COPY`, Redis `RENAME`, Lua scripts, and
key expiry. These operations are validated against the supported FalkorDB
runtime. There is no fallback to destructive in-place rebuilding.

Incremental indexing caches versioned **parse evidence**, not raw source
contents, for unchanged files. It reconstructs the repository's nodes before
resolving relationships, so changed callees do not lose incoming edges from
unchanged callers. This uses temporary disk/memory capacity for a second graph
and performs graph writes for the repository generation; it is not an
in-place, single-file write optimization.

Python calls use lexical scope and explicit import bindings. Ambiguous
cross-file names are not resolved by "last file wins". Other language
relationships remain structural/best-effort, not compiler-level type inference.

## Existing graphs

The default project graph keeps its existing physical name. Existing graph data
remains readable. Index format version 2 adds durable parse metadata; an
incremental update loads legacy files from the authorized repository when that
metadata is absent, rather than trusting a possibly incomplete content hash.
A full rebuild from a valid, nonempty repository repairs historical missing
relationships using the same atomic publication mechanism.

Take and verify a backup before upgrading. Confirm the registered repository
path and container mount before indexing. A visible but empty full scan is
rejected when a graph already contains files. Deliberate removal of the final
file must use explicit incremental deletion paths. Disabled parsers are also
applied to cached parse evidence.

Branch naming, legacy branch handling, and promotion are described in
[Branch Graphs](BRANCH-GRAPHS.md). Never manually treat a graph matching another
project's name as an abandoned branch.

## Diagnosing missing results

1. Check which runtime the UI is connected to. Development and desktop stacks
   have separate graphs and volumes.
2. Check the indexing job's terminal status and error, not only submission
   acceptance. A queued job is not a completed index.
3. Correct unavailable paths or invalid source syntax and retry the failed job.
   A failed generation does not publish its new hashes.
4. For legacy incomplete data, run a full index after confirming the repository
   is mounted and nonempty. Do not clear the current graph first.
5. Verify expected files, symbols, and relationships after completion.

Published generation identifiers are included in retrieval cache keys, so a
successful graph replacement does not reuse the previous generation's results.
Concurrent edits still require a subsequent indexing job; graph snapshots are
not a source-control replacement.

## Backup and deployment boundary

Graph atomicity is not storage durability. Follow
[runtime backup and deployment guidance](runtime-operations.md) for persistent
volumes, snapshots, and restoration. A recently created archive is not proof
that its database snapshot is fresh, and a valid gzip checksum is not a restore
test. Test restoration in an isolated runtime before relying on a backup.
