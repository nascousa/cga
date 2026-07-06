# Azure Policies Beginner Guide - Extracted Notes

Source: `Azure_Policies_Beginner_Guide.pdf`
Extracted on: 2026-06-30

## Executive Summary

This slide deck is an introductory guide for creating Azure Policies, with special emphasis on submitting built-in policies to sovereign cloud environments such as USNat and USSec.

Key points:

- Azure Policy is a governance service used to enforce standards, evaluate resources for compliance, and support security, cost control, configuration consistency, audits, and regulatory requirements.
- Core concepts are policy definitions, initiatives, assignments, and effects.
- Common effects include Audit, Deny, and DeployIfNotExists.
- A policy definition includes properties, if/then policy rule logic, reusable parameters, and an effect in the `then` block.
- Built-in policies are Microsoft-managed; custom policies are created by teams and stored/version-controlled in repositories.
- New policies should generally start from an existing built-in policy, then adjust scope, parameters, or effect and validate in a test subscription.
- USNat and USSec are not part of `AllEnvironments`; policy files must be manually copied into the appropriate folders.
- Sovereign clouds can lag behind Public Azure and may not support the same aliases, effects, or resource provider behavior.
- The same policy GUID must be used across `AllEnvironments`, `USNat`, and `USSec`.
- USNat/USSec accept only the latest version, unlike `AllEnvironments` which supports multiple versions.
- After PR merge, USNat/USSec deployment is a manual policy-team process and can take about one month.
- Best practice is to start from Public Azure, use Audit before Deny, test in sovereign cloud subscriptions, plan for the long deployment pipeline, and keep GUID/version/folder consistency.

## Extracted Slide Content

### Page 1

CREATING AZURE POLICIES - BEGINNER GUIDE

BASED ON UPDATEAZUREPOLICIES.MD

### Page 2

WHAT IS AZURE POLICY?

- Azure Policy is a governance service for enforcing standards
- It evaluates resources for compliance
- Common use cases: security, cost control, configuration enforcement

### Page 3

WHY AZURE POLICY MATTERS

- Prevents non-compliant resource creation
- Ensures consistency across environments
- Supports audits and regulatory compliance

### Page 4

KEY AZURE POLICY CONCEPTS

- Policy Definition - the rule
- Initiative - a collection of policies
- Assignment - where policy is applied
- Effect - what happens if non-compliant

### Page 5

POLICY MANAGEMENT OVERVIEW

- Understanding Common Policy Effects
- Audit: Logging Non-Compliance
- Deny: Blocking Resource Creation
- DeployIfNotExists: Auto-Remediation Mechanism
- Policy Activation and Deactivation

### Page 6

POLICY DEFINITION STRUCTURE

- Properties: displayName, description
- Policy Rule: if / then logic
- Parameters: make policy reusable
- Effect chosen in then block

### Page 7

WHERE POLICIES LIVE

- Built-in policies: Microsoft-managed
- Custom policies: created by you
- Stored and version-controlled in repos

### Page 8

CREATING YOUR FIRST POLICY

- Start from an existing built-in policy
- Modify scope, parameters, or effect
- Validate against test subscription

### Page 9

GETTING ACCESS

### Page 10

POLICY REPO STRUCTURE

Key: `USNat/` and `USSec/` are not included in `AllEnvironments`. You must manually copy policy files into these folders.

`Bleu/` also requires separate handling.

### Page 11

UNDERSTANDING USNAT, USSEC, AND SOVEREIGN CLOUDS

- USNat (Azure Government Secret) and USSec (Azure Government Top Secret) are air-gapped, classified Azure environments operated for U.S. government workloads.
- They are physically isolated from Public Azure: no shared network, no automatic policy sync.
- Public Azure receives features and policies first; USNat/USSec lag behind and require manual deployment.
- Some resource provider aliases and policy effects may be unavailable or behave differently in sovereign clouds.
- Key takeaway: Policy parity gaps can create false compliance confidence if not addressed.

### Page 12

SUBMITTING POLICIES TO USNAT & USSEC

- USNat/USSec are not included in `AllEnvironments`; you must manually copy policy files into their directories.
- File placement: copy your policy JSON into `settings/BuiltInPoliciesV2/USNat/` and `settings/BuiltInPoliciesV2/USSec/`.
- Versioning constraint: USNat/USSec folders accept only the latest version; no multi-version support like `AllEnvironments`.
- Same GUID required: the policy name and ID GUID must be identical across all cloud folders (`AllEnvironments`, `USNat`, `USSec`).
- Alias check: run `UpdateEnvironmentTypesAndAliases.ps1` for each sovereign environment to verify alias availability.
- Pipeline: after PR merge, the policy team runs a manual pipeline; expect about one month for USNat/USSec deployment.

### Page 13

CREATE TO PR COMPLETE (STEPS 1-5)

1. Create Policy File
   - One file per policy under `settings/BuiltInPoliciesV2/`.
   - USNat/USSec: manually copy into `USNat/` and `USSec/` folders.
2. Versioning
   - Add `"version": "1.0.0"` in metadata block.
   - `AllEnvironments`: multiple versions allowed.
   - USNat/USSec: latest version only.
3. Policy Definition
   - Generate GUID for name and ID fields.
   - Same GUID must be used across all cloud folders.
4. Cleanup
   - Remove custom fields, for example `CreatedBy`.
   - Follow naming/style checklist.
5. Localization
   - Run `Sync-BuiltInPolicyVersionsAndResXFiles.ps1`.
   - Yellow items are USNat/USSec-specific requirements.

### Page 14

CREATE TO PR COMPLETE (STEPS 6-10)

6. Aliases, if applicable
   - Run for each environment: `./UpdateEnvironmentTypesAndAliases.ps1 -azureEnv <env>`.
   - Some aliases may not exist in USNat/USSec.
7. Metadata Update
   - Update `PolicyMetadata.json` with GUID, `serviceTreeId`, and `icmRouting`.
8. Create PR
   - Repo -> Pull Requests -> New PR.
   - Example: `[BuiltinPolicy][Low] Update AGC Builtins With Public`.
9. Validation
   - Ensure these tests pass before requesting review:
     - `PolicyDefinitionTests_BuiltInPolicyValidation`
     - `PolicyDefinitionTests_BuiltInPolicyResourceStringTest`
10. PR Completion
   - Confirm requirements in PR comment.
   - Tag reviewers: Namrata Jagasia, Roger Zou, Robert Gao.
   - Complete with squash merge.
   - Yellow items are USNat/USSec-specific requirements.

### Page 15

After PR merge, the deployment pipeline takes about one month to reach USNat and USSec.

This is a manual process run only by the policy team; you cannot trigger it yourself. Plan for this lead time when targeting sovereign cloud deadlines.

### Page 16

USNAT/USSEC COMMON PITFALLS

- Missing aliases: resource provider aliases available in Public Azure may not exist in USNat/USSec. Always run the alias check script per environment.
- Effect incompatibility: some effects, for example DeployIfNotExists, may require resource providers not yet registered in sovereign clouds.
- Forgetting the manual copy: `AllEnvironments` does not deploy to USNat/USSec. If you skip the manual copy step, your policy will not exist in those clouds.
- GUID mismatch: using different GUIDs across cloud folders creates duplicate or conflicting policies. Verify before submitting your PR.
- Version mismatch: USNat/USSec only accept the latest version. Submitting an older version will fail validation.

### Page 17

BEST PRACTICES FOR USNAT/USSEC POLICY SUBMISSION

- Start from the Public Azure definition: always compare your sovereign cloud version against the public policy to ensure intent matches.
- Use Audit before Deny: deploy with Audit effect first to assess impact, then switch to Deny once validated in a test subscription.
- Test in sovereign cloud subscriptions: do not assume Public Azure testing is sufficient; alias and effect behavior can differ.
- Plan for the one-month pipeline: submit early and coordinate with the policy team on deployment timelines for USNat/USSec.
- Keep one GUID, one version, all folders: maintain consistency across `AllEnvironments`, `USNat`, and `USSec` to avoid policy conflicts.

## Recommended Agent: Azure Policy Change Monitor Agent

### Goal

Create an agent that monitors Azure Policy definition, initiative, assignment, and compliance drift across Public Azure and sovereign-cloud policy folders, then produces actionable alerts and review summaries.

### Best-Fit Responsibilities

- Detect policy repository changes:
  - New, deleted, or modified policy JSON files.
  - Missing manual copies for `USNat/` and `USSec/`.
  - GUID mismatches across cloud folders.
  - Version inconsistencies, especially latest-version-only constraints in USNat/USSec.
  - Metadata changes in `PolicyMetadata.json`, including `serviceTreeId` and `icmRouting`.
- Validate policy compatibility:
  - Run alias checks for each target environment.
  - Flag policy effects that may be risky in sovereign clouds, especially `Deny` and `DeployIfNotExists`.
  - Compare Public Azure policy intent against USNat/USSec copies.
- Monitor deployed Azure state:
  - List policy definitions, initiatives, and assignments at subscription or management group scope.
  - Track assignment enforcement mode, parameters, identity, and scope.
  - Track compliance state changes and non-compliant resource counts.
  - Detect drift between repo intent and deployed state.
- Produce operational outputs:
  - PR readiness checklist.
  - Daily or weekly change digest.
  - High-risk policy-change alert.
  - Compliance trend report.
  - One-month sovereign deployment tracking reminder.

### Suggested Architecture

1. Repo watcher
   - Trigger on PR, merge, or scheduled scan.
   - Reads policy folders under `settings/BuiltInPoliciesV2/`.
   - Computes normalized JSON diffs and folder parity checks.
2. Azure state collector
   - Uses Azure Resource Graph / Azure Policy APIs to collect assignments, definitions, initiatives, and compliance state.
   - Runs under a read-only managed identity or service principal.
3. Rule engine
   - Encodes deterministic checks from the guide:
     - `USNat/USSec` file exists when needed.
     - Same GUID across folders.
     - Latest-version-only for USNat/USSec.
     - Audit-before-Deny progression.
     - Alias check completed for each environment.
4. LLM review layer
   - Summarizes the change in human-readable form.
   - Explains why a policy change is risky.
   - Drafts PR comments and reviewer checklist items.
   - Does not make enforcement/remediation changes automatically.
5. Notification and evidence store
   - Sends alerts to Teams, email, GitHub/Azure DevOps PR comments, or IcM depending on severity.
   - Stores snapshots and diff evidence for auditability.

### Recommended Operating Model

- Start in read-only mode.
- Run on every policy PR and once daily on deployed state.
- Require explicit human approval for remediation or assignment changes.
- Separate detection from remediation.
- Treat sovereign-cloud readiness as a first-class gate, not a post-merge reminder.

### Alert Severity

- Critical: `Deny` or `DeployIfNotExists` change without test evidence; assignment scope expanded to management group; missing USSec/USNat copy for required policy.
- High: GUID mismatch, metadata routing missing, alias unavailable in sovereign cloud, enforcement mode changed to `Default` unexpectedly.
- Medium: compliance drift increase, version mismatch, missing PR checklist evidence.
- Low: documentation or naming/style inconsistency.

### Minimum Viable Implementation

- Inputs:
  - Policy repo path.
  - Target folders: `AllEnvironments`, `USNat`, `USSec`, optionally `Bleu`.
  - Azure scopes: subscriptions or management groups.
- Checks:
  - JSON schema/format validation.
  - GUID and version consistency.
  - Folder parity.
  - Effect-risk classification.
  - Azure assignment inventory diff.
- Outputs:
  - Markdown report artifact.
  - PR comment summary.
  - Daily digest.

### Example Agent Instruction

You are the Azure Policy Change Monitor Agent. Your job is to detect risky Azure Policy definition, initiative, assignment, and compliance changes before they cause governance drift. Operate read-only unless explicitly authorized. For every policy change, compare Public Azure, USNat, and USSec intent; verify GUID/version/folder consistency; check alias and effect compatibility; summarize risk; and produce a PR-ready checklist with evidence. Prioritize sovereign-cloud parity gaps, enforcement-mode changes, Deny/DeployIfNotExists changes, and assignment scope expansions.
