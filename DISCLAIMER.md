# CGA Usage Disclaimer

CGA (Context Graph Agent) is provided for developer productivity, local-first
project indexing, graph analysis, MCP-compatible retrieval, and work briefing
automation. Use of CGA is subject to the Apache License, Version 2.0, including
its warranty and liability limitations. This document highlights practical usage
risks and does not replace the license text.

This document is not legal, security, compliance, or professional advice.

## No Warranty

CGA is provided on an "as is" and "as available" basis. The maintainers do not
guarantee that CGA will be error-free, uninterrupted, secure, suitable for a
particular purpose, or compatible with every repository, model, toolchain,
deployment environment, or third-party service.

## User Responsibility

Users and operators are responsible for:

- Reviewing and accepting the Apache License 2.0 and applicable third-party
  licenses before use or redistribution.
- Configuring authentication, authorization, network exposure, TLS, firewall
  rules, and MCP access controls for their environment.
- Changing default credentials and protecting tokens, OAuth secrets, private
  keys, runtime databases, backups, audit logs, and generated artifacts.
- Confirming they have the right to index, process, store, export, or share any
  repository content, metadata, work activity, or operational data provided to
  CGA.
- Testing backups, restores, upgrades, and deployment changes before relying on
  CGA for important workflows.
- Reviewing automated actions, scheduled jobs, MCP tool calls, generated
  summaries, and graph analysis results before acting on them.

## Sensitive Data And Privacy

CGA may process or store repository paths, source symbols, imports, calls,
lightweight data-flow metadata, user records, project tokens, audit events,
work briefing data, runtime configuration, and backup snapshots. Do not use CGA
with confidential, regulated, customer, or third-party data unless you have
validated the deployment controls, retention policy, access boundaries, and
legal basis for that processing.

Do not include real secrets, production tokens, private keys, credentials, or
confidential third-party data in public issues, logs, support bundles, example
repositories, screenshots, or generated release artifacts.

CGA's harness-learning feature stores bounded project-scoped lesson summaries,
outcomes, optional harness/task/session identifiers, and caller-supplied
activity references. New lessons remain unapproved candidates, and CGA does
not verify that referenced activities exist or belong to the project. Common
credential patterns are rejected, but this is not a substitute for review:
never submit raw prompts, completions, source files, or unredacted tool
arguments, and verify retrieved lessons against current project evidence before
relying on them.

## Automation And AI-Assisted Output

CGA can support AI-assisted development workflows by indexing project context
and exposing retrieval or reporting tools. Retrieved context, generated
summaries, dependency findings, graph analysis, and scheduled automation results
may be incomplete, outdated, or inaccurate. Human review remains required for
security decisions, production deployment, compliance review, release approval,
legal review, and other high-impact actions.

## Third-Party Services And Dependencies

CGA may rely on third-party packages, container images, browser-delivered
assets, package registries, GitHub, OAuth providers, Azure DevOps integrations,
CDNs, databases, or other external systems depending on configuration and
deployment mode. Those services and components are governed by their own terms,
licenses, security practices, availability, and support policies.

Review `THIRD_PARTY_NOTICES.md`, generated SBOM/license reports, and exact
release artifacts before redistributing CGA or deploying it in a shared
environment.

## Trademarks And Acknowledgements

Third-party names, marks, and logos are used for identification,
compatibility, integration, or acknowledgement purposes only. Unless separately
stated in writing, references to Microsoft or any other organization do not
imply endorsement, sponsorship, ownership, maintenance, or affiliation.

## Support And Changes

Published versions, examples, defaults, APIs, integrations, and documentation
may change over time. Users should pin versions for repeatable deployments,
review release notes before upgrades, and maintain their own rollback and
incident response procedures.