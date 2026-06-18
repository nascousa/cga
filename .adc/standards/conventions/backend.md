# Backend Application Policy

## Default API Runtime
- **FastAPI Default**: New web application backends SHOULD use FastAPI as the default API framework unless project requirements explicitly justify another runtime.
- **Typed Contracts**: FastAPI request and response models MUST use explicit Pydantic schemas for externally visible API contracts.
- **Health Endpoint**: FastAPI services MUST expose a lightweight `/health` endpoint suitable for Docker Compose health checks and local smoke validation.
- **Async Boundaries**: Use async handlers for I/O-heavy endpoints and keep blocking CPU-heavy work outside request handlers unless explicitly bounded.

## Default Web App Data Pairing
- **PostgreSQL Default**: New web application backends SHOULD use PostgreSQL as the default relational database.
- **pgvector Default**: If the application includes semantic search, recommendations, embeddings, RAG, or AI-assisted retrieval, PostgreSQL with `pgvector` is the default vector store.
- **Migration Discipline**: Schema changes MUST be expressed through repeatable migrations and include rollback or forward-fix notes.
