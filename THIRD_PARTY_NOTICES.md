# Third-Party Notices

CGA uses third-party open source packages and services. This file summarizes
the direct dependencies used by the source tree. It is not a substitute for the
exact license files and notices shipped by resolved dependencies, package
managers, or container base images.

CGA itself is licensed under the Apache License, Version 2.0. Third-party
components remain governed by their own licenses and notices.

Before distributing a release artifact, review the exact dependency set from the
resolved Python environment, `src/viewer/package-lock.json`, browser-delivered
assets, and any container image manifests used for that release.

## Python Backend Direct Dependencies

The backend dependency manifest is `requirements.txt`.

| Package | Purpose | Declared license note |
| --- | --- | --- |
| FastAPI | Web API framework | MIT |
| Uvicorn | ASGI server | BSD-3-Clause |
| MCP Python SDK | Model Context Protocol server/client SDK | MIT |
| Pydantic | Data validation and models | MIT |
| bcrypt | Password hashing | Apache-2.0 |
| python-jose | JOSE/JWT support | MIT |
| asyncpg | PostgreSQL async driver | Apache-2.0 |
| redis-py | Redis client | MIT |
| FalkorDB Python client | FalkorDB client access | Verify package metadata for the resolved version |
| tree-sitter | Python bindings for structural source parsing | MIT |
| tree-sitter-language-pack | Prebuilt tree-sitter language parsers | MIT OR Apache-2.0 |
| tree-sitter-c-sharp | C# parser grammar | MIT |
| tree-sitter-embedded-template | Embedded-template parser grammar | MIT |
| tree-sitter-yaml | YAML parser grammar | MIT |
| sentence-transformers | Semantic embeddings | Verify package metadata for the resolved version |
| NumPy | Array computing | BSD-3-Clause and related package notices |
| structlog | Structured logging | MIT OR Apache-2.0 |
| prometheus-client | Metrics export | Verify package metadata for the resolved version |
| pytest | Test runner | MIT |
| pytest-cov | Coverage plugin | MIT |
| pytest-asyncio | Async test support | Apache-2.0 |
| HTTPX | HTTP client for tests/integrations | BSD-3-Clause |
| Black | Code formatter | MIT |
| Ruff | Linter | MIT |
| Mypy | Type checking | MIT |
| pip-audit | Dependency vulnerability audit | Verify package metadata for the resolved version |

## Viewer Direct Dependencies

The browser viewer dependency manifest is `src/viewer/package.json`, with exact
resolved packages in `src/viewer/package-lock.json`.

| Package | Purpose | Declared license note |
| --- | --- | --- |
| Graphology | Graph data structure | MIT |
| Sigma.js | WebGL graph renderer | MIT |
| Vite | Viewer build tooling | MIT |

The viewer lockfile also includes transitive build dependencies. Preserve any
required transitive notices when redistributing bundled assets.

## Browser-Delivered Assets, CDNs, Fonts, And Icons

The tracked viewer source currently uses inline SVG icons and does not include
Font Awesome, Lucide, Codicons, Google Fonts, or other external icon/font asset
packages as project dependencies.

The unbundled viewer HTML imports Graphology and Sigma.js through the `esm.sh`
import map for browser execution. Release builds should either preserve the
upstream license notices for those packages or ship a generated license report
for the bundled viewer assets.

FastAPI's default interactive API documentation endpoints may load Swagger UI,
ReDoc, and related web assets from their upstream CDN URLs when those endpoints
are enabled. If CGA vendors, customizes, or redistributes those assets directly,
include their exact license notices in the release package.

If Font Awesome is added in a future change, record the exact product line and
license terms. Font Awesome Free generally separates licensing by asset type:

| Font Awesome Free component | Common license note |
| --- | --- |
| Icons | CC BY 4.0 |
| Fonts | SIL Open Font License 1.1 |
| Code | MIT |

Font Awesome Pro is commercial software and must not be treated as an open
source dependency unless the project has a valid separate commercial license and
distribution rights.

## Runtime Services And Images

Depending on the compose profile or release bundle, CGA may run with PostgreSQL,
Redis-compatible services, FalkorDB, Nginx, Python, Node.js, Alpine Linux,
Debian, and other container base image contents. Those images are governed by
their own upstream licenses and notices. Review the exact image tags used by the
release before distributing a packaged bundle.

Known image and base-image references in the current source tree include:

| Image or source | Purpose | Notice requirement |
| --- | --- | --- |
| `python:3.12-slim` | API/dev/prod base image | Preserve Python, Debian, and bundled package notices for distributed images. |
| `node:20-alpine` | Frontend build stage | Preserve Node.js, Alpine Linux, and bundled package notices when distributing derived images. |
| `nginx:1.27-alpine` | Frontend/gateway runtime | Preserve Nginx, Alpine Linux, and bundled package notices. |
| `redis:7-alpine` | Queue/cache runtime | Preserve Redis, Alpine Linux, and bundled package notices. |
| `postgres:16-alpine` | Backup sidecar / PostgreSQL client tools | Preserve PostgreSQL, Alpine Linux, and bundled package notices. |
| `pgvector/pgvector:pg16` | PostgreSQL with pgvector extension | Preserve PostgreSQL, pgvector, base image, and bundled package notices. |
| `falkordb/falkordb:latest` | Graph database runtime | Preserve FalkorDB image and bundled package notices. |
| `ghcr.io/nascousa/cga-api` | Published CGA API/runtime image | Include CGA license package plus all resolved image notices. |

Dockerfiles also install operating-system packages such as `curl`, `git`,
`postgresql-client`, and `wget`. Their licenses are inherited from the resolved
Debian or Alpine package repositories in the exact image build.

## SBOM And Generated License Reports

For formal releases, maintainers should ship a generated SBOM or license report
alongside this hand-maintained summary. Recommended artifacts include:

- CycloneDX or SPDX SBOM for the Python environment.
- npm package license report for `src/viewer/package-lock.json`.
- Container image SBOM/license report for each published image tag.
- A copied `licenses/` directory when a build tool can collect exact upstream
  license texts.

This file should remain the human-readable map; generated SBOM/license outputs
should provide exact resolved package versions and transitive dependency data.

## Notice Maintenance

When a direct dependency, browser asset, icon/font package, model, image,
template, base image, or externally hosted runtime asset changes, update this
file in the same pull request. If an automated SBOM or license report is
generated for a release, ship it alongside this notice rather than replacing
this project-level summary.
