# Azure Policy Change Monitor Agent Recommendation

Date: 2026-06-30
Source notes: `Azure_Policies_Beginner_Guide_extracted.md`

## Recommendation

Build an **Azure Policy Change Monitor Agent** as a mostly deterministic monitoring agent with an LLM-assisted review layer.

The agent should not rely on the LLM to decide compliance. It should use code-based checks for policy JSON, folder parity, GUID/version consistency, assignment drift, and compliance drift. The LLM layer should summarize findings, explain risk, and draft PR comments or operational digests.

## Primary Goals

- Detect risky Azure Policy repository changes before merge.
- Detect drift between repository intent and deployed Azure Policy state.
- Treat Public Azure, USNat, and USSec parity as a first-class review gate.
- Produce evidence-backed PR comments, daily digests, and high-risk alerts.
- Stay read-only by default; require human approval for remediation or assignment changes.

## What To Monitor

### Repository Changes

- New, modified, or deleted policy JSON files.
- Missing manual copies into `USNat/` and `USSec/`.
- GUID mismatch across `AllEnvironments`, `USNat`, and `USSec`.
- Version mismatch, especially because `USNat/` and `USSec/` accept only the latest version.
- Risky `effect` changes, especially `Deny` and `DeployIfNotExists`.
- Metadata changes in `PolicyMetadata.json`, including `serviceTreeId` and `icmRouting`.
- Missing localization or validation evidence.

### Azure State Changes

- Policy definition changes.
- Policy initiative changes.
- Policy assignment changes.
- Assignment scope expansion.
- `enforcementMode` changes.
- Assignment parameter changes.
- Managed identity changes on assignments.
- Compliance state changes and non-compliant resource count trends.

## Proposed Architecture

1. **Trigger Layer**
   - Pull request webhook or CI job for policy repo changes.
   - Scheduled daily scan for deployed Azure state.
   - Optional manual run for release or pre-review checks.

2. **Repo Scanner**
   - Parses policy JSON files.
   - Normalizes JSON before diffing.
   - Checks folder parity across `AllEnvironments`, `USNat`, `USSec`, and optional sovereign folders such as `Bleu`.
   - Extracts GUID, version, effect, aliases, parameters, metadata, and assignment-related references.

3. **Azure State Collector**
   - Uses Azure Resource Graph, Azure Policy APIs, Policy Insights, and Activity Log.
   - Reads definitions, initiatives, assignments, compliance state, assignment identities, enforcement mode, parameters, and scopes.
   - Runs with read-only identity permissions.

4. **Rule Engine**
   - Encodes deterministic checks from the guide:
     - Same GUID across cloud folders.
     - Latest-version-only rule for `USNat/` and `USSec/`.
     - Required manual copy for sovereign cloud folders.
     - Audit-before-Deny progression.
     - Alias availability check per environment.
     - High-risk detection for `Deny`, `DeployIfNotExists`, and scope expansion.

5. **LLM Review Layer**
   - Summarizes findings.
   - Explains impact and recommended next action.
   - Drafts PR comments and reviewer checklist items.
   - Produces daily or weekly digest text.
   - Must cite evidence from scanner output, not invent facts.

6. **Storage and Notifications**
   - Store snapshots and reports for auditability.
   - Send PR comments, Teams messages, email, or IcM alerts depending on severity.
   - Store trend data in Log Analytics, Azure Table Storage, PostgreSQL, or the existing ContextGraph/CGA data store.

## Recommended Technology Stack

### MVP Stack

- **Language:** Python, because the existing ContextGraphAdmin workspace is Python-oriented and policy scanning is JSON/file/API heavy.
- **Agent framework:** Microsoft Agent Framework for the LLM-assisted reviewer, with deterministic Python tools for scanning and Azure state collection.
- **Azure APIs:** Azure SDK for Python plus Azure Resource Graph / Azure Policy / Policy Insights / Activity Log queries.
- **Execution:** GitHub Actions or Azure DevOps Pipeline for PR-time checks; Azure Functions Timer Trigger or Azure Container Apps Job for scheduled scans.
- **Identity:** Managed Identity in Azure, or workload identity federation from CI, with read-only Azure Policy and Resource Graph permissions.
- **Storage:** Log Analytics for operational telemetry plus a small snapshot store such as Azure Storage, PostgreSQL, or the existing CGA store.
- **Notifications:** PR comments first, Teams webhook second, IcM only for critical production-impacting drift.

### Production Stack

- **Runtime:** Azure Container Apps Job if the scanner needs longer-running or dependency-heavy execution; Azure Functions if scans are small and event/timer driven.
- **Orchestration:** Microsoft Agent Framework workflow with explicit tool calls.
- **Model host:** Microsoft Foundry for the summarization/review model when enterprise governance, model deployment control, tracing, and evaluation are needed.
- **Observability:** Application Insights and Log Analytics.
- **Security:** Managed Identity, least-privilege RBAC, no stored user credentials, read-only default mode.

## Why This Stack

- Policy validation is deterministic; code should own correctness.
- LLMs are useful for summarization, risk explanation, and reviewer-facing comments, not as the source of truth.
- Python is strong for repo scanning, JSON normalization, Azure SDK calls, and quick CI integration.
- Microsoft Agent Framework provides a clean way to expose scanner functions as tools and keep the LLM constrained to evidence-backed review.
- Azure Functions or Container Apps Jobs provide straightforward scheduled execution in Azure.
- Resource Graph and Policy Insights are the right data sources for deployed-state and compliance drift monitoring.

## Minimum Viable Agent

1. Parse changed policy JSON files in a PR.
2. Check GUID consistency across `AllEnvironments`, `USNat`, and `USSec`.
3. Check version rules for sovereign cloud folders.
4. Detect risky effect changes.
5. Check required metadata fields.
6. Produce a Markdown report.
7. Post a PR comment with pass/fail checks and evidence.

## Next Iteration

1. Add Azure deployed-state inventory.
2. Compare repo intent against actual definitions and assignments.
3. Add compliance trend tracking.
4. Add Teams/IcM alerts by severity.
5. Add LLM-generated daily digest and PR risk summary.
6. Add evaluation tests for hallucination resistance: every LLM statement must trace back to scanner evidence.

## Example Agent Instruction

You are the Azure Policy Change Monitor Agent. Your job is to detect risky Azure Policy definition, initiative, assignment, and compliance changes before they cause governance drift. Operate read-only unless explicitly authorized. For every policy change, compare Public Azure, USNat, and USSec intent; verify GUID/version/folder consistency; check alias and effect compatibility; summarize risk; and produce a PR-ready checklist with evidence. Prioritize sovereign-cloud parity gaps, enforcement-mode changes, Deny/DeployIfNotExists changes, and assignment scope expansions.
