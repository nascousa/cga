# Contributing To CGA

Thank you for considering a contribution to CGA (Context Graph Agent). The goal
of this guide is to keep contributions easy to review, safe to merge, and clear
from a licensing perspective.

## License Of Contributions

Unless a separate written agreement says otherwise, contributions submitted to
CGA are accepted under the Apache License, Version 2.0. By submitting a pull
request, patch, or other contribution, you confirm that you have the right to
contribute the work and license it under Apache-2.0.

## Contribution Workflow

1. Fork or branch from the current development branch.
2. Keep changes focused on one issue or feature.
3. Do not commit secrets, private keys, `.env` files, runtime databases, local
   backups, or generated release bundles.
4. Add or update tests when behavior changes.
5. Update documentation when user-visible behavior, setup, deployment, or
   licensing details change.
6. Update `THIRD_PARTY_NOTICES.md` when dependencies or base images change.
7. Open a pull request with a concise summary and validation notes.

## Documentation Language and Filenames

All repository documentation must be written in English, including reports,
deployment records, internal notes, and documentation templates. Use English
titles and filenames as well; do not add translated document variants.

Versioned deliverables use `<English-Title>-v<MAJOR.MINOR.PATCH>-en.<ext>`.
Keep the filename and document version consistent. Stable entry/control files
such as `README.md`, `CONTRIBUTING.md`, `AGENTS.md`, and `SKILL.md` retain their
established names. Do not overwrite released document versions.

## Dependency Changes

New dependencies should be justified in the pull request and checked for:

- License compatibility with the Apache-2.0-licensed CGA distribution.
- Known vulnerabilities and project CVE/CVSS policy.
- Impact on Docker image size, startup time, and local-first usage.

## Code Review Expectations

Maintainers prioritize correctness, security, maintainability, and clear user
impact. A pull request may be asked to split unrelated changes, add tests, or
update notices before merge.
