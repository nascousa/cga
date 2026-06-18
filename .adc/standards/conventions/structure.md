# Project Structure & Layout Conventions

## Directory Layout

All ADC projects MUST follow this standard root structure:

```text
project-root/
├── .adc/                    # Autonomous Development Constitution (hidden)
├── src/                     # All source code
│   ├── dist/                # Compiled/bundled output (git-ignored)
│   │   ├── release/         # Final production artifacts
│   │   ├── staging/         # Pre-production build output
│   │   └── build/           # Intermediate build cache
│   ├── contextgraph-mcp/              # CGA MCP Server implementation
│   ├── contextgraph-edge-agent/       # ContextGraph Edge Agent implementation
│   ├── scripts/             # Utility scripts for building, deploying, local dev
│   └── ... (application modules)
├── docs/                    # User-facing and project documentation
├── tests/                   # Automated tests (mirrors src structure)
├── .github/                 # GitHub workflows, issue templates, PR templates
├── .gitignore              # Git exclusion rules
└── ... (config files: package.json, tsconfig.json, etc.)
```

## Compiled Assets (src/dist/)

**STRICT RULE**: All compiled or bundled software output MUST be directed to `src/dist/`.

- **Subdirectory Structure**:
  - `src/dist/release/` — Final production artifacts (versioned builds, executable bundles)
  - `src/dist/staging/` — Pre-production builds for testing and validation
  - `src/dist/build/` — Intermediate build cache, temporary compilation artifacts

- **Git Ignore**: `src/dist/` MUST be added to `.gitignore` to prevent build artifacts from being committed.

- **Rationale**: Keeping all build outputs under `src/` maintains a clear separation between source code and generated assets while keeping dependencies visible and organized.

## Environment Variables

- `.env.example` — Template file with all required environment variables and dummy values (REQUIRED in git)
- `.env` — Actual environment configuration (git-ignored, never committed)

**RULE**: NO real secrets or API keys shall ever be written to `.env`. All credentials must be injected at runtime via secure secret managers (e.g., AWS Secrets Manager, HashiCorp Vault).

## Source Code Integrity

All core business logic MUST reside in `src/`. Root-level software logic (other than standard config files) is strictly forbidden.

Approved root-level files only:
- `package.json`, `package-lock.json`
- `tsconfig.json`, `.eslintrc.*`, `prettier.config.*`
- `README.md`, `LICENSE`
- `.github/`, `.gitignore`, `.gitattributes`
- `.adc/`
- Configuration for tools (`.nvmrc`, `Dockerfile`, `docker-compose.yml`)

## Utility Scripts

All supplementary bash, Python, or Node.js scripts for building, deploying, or local development MUST be placed in `src/script/`.

**Examples**:
- `src/script/build.sh` — Compilation pipeline
- `src/script/migrate.js` — Database migration runner
- `src/script/deploy.sh` — Production deployment script
- `src/script/seed.py` — Development data seeding

## Git Push Protocol

- Default protocol for repository check-in and push operations MUST be HTTPS.
- If HTTPS push fails due to transient auth/network issues, SSH is the required fallback.
- Teams MUST avoid protocol switching outside this order unless a maintainer approves an exception in the same change set.

## Documentation

- `docs/` — All user-facing, API, and project documentation
- `.adc/` — Internal AI governance and conventions (not published)

Keep these strictly separated.

## Testing

All automated tests MUST be placed in `tests/` at project root, mirroring `src/` structure:

```text
tests/
├── unit/
│   ├── contextgraph-mcp/
│   ├── contextgraph-edge-agent/
│   └── ... (mirrors src structure)
├── integration/
├── e2e/
└── fixtures/
```

**RULE**: Tests must NEVER be mixed within application source files in `src/`.


