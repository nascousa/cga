# Data Engineering Policy

## PostgreSQL Vector Policy (`pgvector`)
- **Standard Vector Store**: For production semantic search, PostgreSQL with `pgvector` is the default vector persistence layer.
- **Default Web App Store**: Web application projects SHOULD default to PostgreSQL plus `pgvector` when they need relational data and vector retrieval in the same product surface.
- **Model Consistency**: Each embedding column MUST be tied to a single embedding model/version and fixed vector dimension.
- **Index Strategy**: Use `HNSW` or `IVFFlat` indexes for vector columns based on latency/recall requirements. Brute-force scans are not allowed for production-scale datasets.
- **Query Constraints**: All vector queries MUST enforce explicit `top_k` limits and include metadata filters when available.
- **Distance Metric Discipline**: Use a single declared similarity metric per index/query path (cosine, inner product, or L2) and do not mix metrics in the same retrieval pipeline.

## SQLite Vector Policy (`sqlite-vec`)
- **Scope**: `sqlite-vec` is allowed for local development, offline testing, and lightweight edge use cases.
- **Production Guardrail**: Do not use `sqlite-vec` as the primary retrieval backend for high-concurrency production workloads unless explicitly approved by architecture review.
- **Migration Readiness**: Local vector schema and retrieval contract MUST be compatible with planned migration to `pgvector`.

## Graph Database Policy
- **When to Use Graph DB**: Use graph databases for deep relationship traversal, variable-depth path queries, and graph-native analytics.
- **Query Interface**: Graph workloads MUST use the official graph connector and graph query language (Cypher/Gremlin) rather than recursive SQL workarounds.
- **Identity Consistency**: Node/edge identifiers MUST be stable and map cleanly to relational primary keys where dual storage exists.
- **Write Safety**: Graph mutation paths MUST be idempotent and support retry-safe behavior for at-least-once delivery.

## Cross-Store Data Governance
- **Ownership Rule**: For each entity, define a single source of truth (relational, vector, or graph) and document replication/sync direction.
- **Backfill Rule**: Any re-embedding or graph backfill process MUST be versioned, resumable, and observable.
- **Deletion Rule**: Data deletion requests MUST propagate consistently across relational, vector, cache, and graph stores.
