# Security Policy

CGA is local-first developer infrastructure, but it can hold sensitive project
metadata, tokens, audit logs, and repository context. Please report security
issues responsibly.

## Reporting A Vulnerability

Do not open a public issue for an unpatched vulnerability. Use a private GitHub
Security Advisory when available, or contact the maintainers through the
repository owner's published contact path.

Please include:

- Affected version or commit.
- A clear description of the issue.
- Steps to reproduce, proof of concept, or logs when safe to share.
- Expected impact and any known mitigations.

Do not include real secrets, production tokens, private keys, or confidential
third-party data in the report.

## Supported Versions

Security fixes are prioritized for the current published release line and the
active development branch. Older local builds should be upgraded before exposing
CGA beyond localhost.

## Security Baselines

- Change default admin credentials and `JWT_SECRET_KEY` before any non-local
  deployment.
- Keep MCP access protected by project-scoped tokens.
- Do not commit `.env`, runtime databases, backups, deploy keys, or generated
  local artifacts.
- Use only Authenticode-signed CGA-Relay release artifacts and verify the
  published SHA-256 file before execution. Windows account sessions are
  protected for the current user with DPAPI.
- Keep credential values out of Relay config files. The Relay accepts only
  documented non-secret config keys, rejects unknown or duplicate assignments,
  and validates credential environment variable names.
- Relay loopback HTTP responses are capped at 8 MiB with 30-second read and
  write timeouts. The local Settings listener separately caps request headers
  and bodies at 64 KiB each, applies the same timeouts, rejects ambiguous body
  framing, and validates state-changing POST requests against its exact
  loopback Host and browser Origin. MCP payloads, settings request bodies,
  backend HTTP payloads, and untrusted response headers are logged only as byte
  counts and status codes where available; payload content and payload hashes
  are not stored.
- Treat binary hardening and symbol stripping as defense in depth, not as a
  guarantee that native code cannot be analyzed or decompiled.
- Run dependency and container vulnerability checks before public releases.
