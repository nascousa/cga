# Azure Deployment Plan

> **Status:** Deployed

Generated: 2026-07-27T15:49:54-07:00

Deployed: 2026-07-27T16:11:18-07:00

---

## 1. Project Overview

**Goal:** Deploy and activate the Azure Policy Change Monitor with a durable, read-only managed-identity proxy, then run the monitor every 30 minutes.

**Path:** Add Components

---

## 2. Requirements

| Attribute | Value |
|-----------|-------|
| Classification | Development |
| Scale | Small: 0-1 proxy replica |
| Budget | Cost-Optimized |
| Subscription | AzurePG-Fundamentals-Dev1 (`40d9a853-9ece-49c7-84eb-3f9896cd2a27`), user confirmed |
| Location | East US, user confirmed |
| Resource group | Existing `azurepg-icm-automation` |
| Security boundary | Read-only ARM operations through a dedicated user-assigned managed identity |

---

## 3. Components Detected

| Component | Type | Technology | Path |
|-----------|------|------------|------|
| Azure Policy monitor | Extension worker | Python standard library | `extensions/azure-policy-monitor/` |
| Managed read proxy | API | Python standard library HTTP server | `extensions/azure-policy-monitor/src/policy_monitor/proxy_server.py` |
| Extension configuration | API/UI | FastAPI backend and static frontend | `src/backend/extensions/registry.py`, `src/frontend/index.html` |

---

## 4. Recipe Selection

**Selected:** Bicep plus Azure CLI

**Rationale:** Reuse the existing IWM Azure Container Registry and Container Apps environment without adding AZD ownership or replacing existing infrastructure. Deployment is split into identity/RBAC and application phases so AcrPull can propagate before the image is used.

---

## 5. Architecture

**Stack:** Containers

### Service Mapping

| Component | Azure Service | SKU |
|-----------|---------------|-----|
| Managed read proxy | Azure Container Apps | Existing Consumption environment, 0-1 replicas, 0.25 CPU, 0.5 GiB |
| Proxy image | Azure Container Registry | Existing registry |
| Azure authentication | User-assigned managed identity | New dedicated identity |

### Supporting Services

| Service | Purpose |
|---------|---------|
| Existing Container Apps environment | Hosts the scale-to-zero proxy |
| Existing Azure Container Registry | Stores the immutable proxy image |
| Managed Identity | Obtains ARM tokens without credentials |
| Azure RBAC Reader | Permits subscription-scoped read operations only |
| Azure RBAC AcrPull | Permits image pull only on the existing registry |
| Container App secret store | Holds the generated proxy request key |

### Security Constraints

- Fixed HTTPS endpoint: `/v1/azure-policy/query`.
- Fixed target subscription and allowlisted read operations.
- No compliance operation and no Azure write operation.
- Shared request key exists only in the Container App secret store and ignored local environment configuration.
- No client secret, certificate, connection string, or registry password.
- HTTPS only, non-root container, immutable base image digest, bounded retries, response size, collection size, and Activity Log lookback.
- Subscription Reader is the broadest permission and is required to read policy resources and Activity Log across the selected subscription.

---

## 6. Provisioning Limit Checklist

| Resource or Quota | Number to Deploy | Current Usage | Total After Deployment | Limit/Quota | Source |
|-------------------|------------------|---------------|------------------------|-------------|--------|
| `Microsoft.App/containerApps` in reused environment | 1 | 2 apps | 3 apps | Capacity enforced by environment cores | Azure Resource Graph, all accessible subscriptions considered and filtered to target subscription/environment |
| Managed Environment Consumption Cores | 0.25 maximum active cores | 1 core | 1.25 cores | 100 cores | `az containerapp env list-usages` |
| `Microsoft.ManagedIdentity/userAssignedIdentities` in East US | 1 | 3 identities | 4 identities | Entra directory object quota below | Azure Resource Graph, target subscription |
| Microsoft Entra directory objects | 1 service principal | 32,639,410 | 32,639,411 | 50,000,000 | Microsoft Graph `organization.directorySizeQuota` |
| `Microsoft.Authorization/roleAssignments` | 2 | 192 | 194 | 4,000 | `az role assignment list --all`; Microsoft Learn fixed subscription limit |
| `Microsoft.App/managedEnvironments` in East US | 0, reuse existing | Existing environment retained | No change | 50 | Azure quota CLI `ManagedEnvironmentCount` |

**Status:** All planned resources are within limits.

---

## 7. Execution Checklist

### Phase 1: Planning

- [x] Analyze workspace
- [x] Gather requirements
- [x] Confirm subscription and location with user
- [x] Prepare resource inventory
- [x] Fetch quotas and validate capacity using the Azure quota CLI first
- [x] Scan codebase
- [x] Select Bicep recipe
- [x] Plan architecture
- [x] User approved reuse and deployment: "确认复用并部署"

### Phase 2: Execution

- [x] Research Container Apps, managed identity, ACR, and RBAC components
- [x] Generate Bicep infrastructure files
- [x] Generate the proxy Dockerfile
- [x] Apply least-privilege and secret-handling constraints
- [x] Build and smoke-test the proxy image locally
- [x] Update plan status to `Ready for Validation`

### Phase 3: Validation

- [x] Plan status is `Ready for Validation`
- [x] Invoke azure-validate skill
- [x] All validation checks pass
  - [x] Focused extension tests and Ruff checks pass
  - [x] Both Bicep entry points compile
  - [x] Identity deployment what-if contains exactly three creates and no updates/deletes
  - [x] Proxy image builds and runs as a non-root user
  - [x] Local health endpoint returns `ok`
  - [x] Local unauthenticated proxy request returns HTTP 401
  - [x] Static RBAC review confirms only Reader and AcrPull
  - [x] Deployment files contain no secret value
- [x] Update plan status to `Validated`
- [x] Record validation proof below

### Phase 4: Deployment

- [x] Invoke azure-deploy skill
- [x] Deploy identity and RBAC stage
- [x] Verify Reader and AcrPull propagation
- [x] Build the immutable image in the existing ACR
- [x] Preview and deploy the Container App stage
- [x] Verify HTTPS health and all allowlisted live read operations
- [x] Configure local monitor with the environment-only proxy key
- [x] Run baseline and no-drift verification
- [x] Create and manually execute the 30-minute extension task
- [x] Update plan status to `Deployed`

---

## 8. Validation Proof

| Check | Command Run | Result | Timestamp |
|-------|-------------|--------|-----------|
| Focused lint | `python -m ruff check --select E,F,W --ignore E501 <task files>` | Pass | 2026-07-27T15:53:51-07:00 |
| Focused tests | `python -m pytest -q --no-cov <9 Azure Policy monitor test files>` | Pass, 48 tests | 2026-07-27T15:53:51-07:00 |
| Bicep compilation | `az bicep build --file <identity/app> --stdout` | Pass for both entry points | 2026-07-27T15:53:51-07:00 |
| Identity preview | `az deployment sub what-if ... identity.bicep` | Pass: 3 creates, 0 updates, 0 deletes | 2026-07-27T15:53:51-07:00 |
| Static RBAC review | Parse task Bicep role definition GUIDs | Pass: Reader and AcrPull only | 2026-07-27T15:53:51-07:00 |
| Secret scan | Scan deployment artifacts for inline proxy key assignment | Pass: no inline key | 2026-07-27T15:53:51-07:00 |
| Container verification | Build image; assert UID; call health and unauthenticated query | Pass: UID 65532, health `ok`, HTTP 401 | 2026-07-27T15:53:51-07:00 |
| Identity and RBAC deployment | `az deployment sub create ... identity.bicep` | Pass: UAMI plus Reader and ACR-scoped AcrPull created | 2026-07-27 |
| Live RBAC propagation | Query assignments for principal `4eb74170-7444-40bb-965d-0242a13e13af` | Pass: Reader and AcrPull visible at their exact scopes | 2026-07-27 |
| ACR image build | `az acr build ... Dockerfile.proxy` and resolve manifest metadata | Pass: `sha256:0ee00e3238f32df8152c61890f054aedfe72100639293c522a087dea406160f2` | 2026-07-27T15:57:17-07:00 |
| Application preview | Resource-group what-if with immutable image and secure key parameter | Pass: 1 create, 0 modifies, 0 deletes | 2026-07-27 |
| Container App deployment | `az deployment group create ... app.bicep` | Pass: `cga-azure-policy-proxy`, revision `cga-azure-policy-proxy--k0nm12w` ready | 2026-07-27T16:02:47-07:00 |
| Live proxy boundary | Call all allowlisted operations, no-key request, and compliance request | Pass: four reads HTTP 200, no key HTTP 401, compliance denied | 2026-07-27 |
| Baseline and drift | Run platform extension twice | Pass: run IDs 6 and 7, stable inventory, 0 findings | 2026-07-27T16:09:27-07:00 |
| Scheduled execution | Create and manually run 30-minute extension task | Pass: task `ZCOTRCUE`, schedule ID 4, extension run ID 8 | 2026-07-27T16:11:18-07:00 |

**Validated by:** azure-validate skill

**Validation timestamp:** 2026-07-27T15:53:51-07:00

---

## 9. Files Generated

| File | Purpose | Status |
|------|---------|--------|
| `.azure/deployment-plan.md` | Deployment source of truth | Complete |
| `deploy/azure-policy-proxy/identity.bicep` | UAMI, Reader, and AcrPull stage | Complete |
| `deploy/azure-policy-proxy/acr-pull.bicep` | Existing ACR scoped AcrPull assignment | Complete |
| `deploy/azure-policy-proxy/app.bicep` | Managed-identity Container App stage | Complete |
| `extensions/azure-policy-monitor/Dockerfile.proxy` | Pinned non-root proxy image | Complete |
| `extensions/azure-policy-monitor/.dockerignore` | Minimal image build context | Complete |
| `docs/runtime-operations.md` | Deployment, validation, scheduling, and key-rotation runbook | Complete |

---

## 10. Operating State

- Proxy endpoint: `https://cga-azure-policy-proxy.kindtree-a8b25993.eastus.azurecontainerapps.io`
- Immutable image: `azurepgicmwfmdevhrcqsoldbnqao.azurecr.io/cga-azure-policy-proxy@sha256:0ee00e3238f32df8152c61890f054aedfe72100639293c522a087dea406160f2`
- Local runtime: `cga-desktop-api` at `http://localhost:18001`
- Schedule: ID 4, task `ZCOTRCUE`, enabled every 30 minutes
- Monitor scope: subscription `40d9a853-9ece-49c7-84eb-3f9896cd2a27`, 120-minute Activity Log lookback, 90 snapshots, compliance disabled

Use the Azure Policy operations section in `docs/runtime-operations.md` for health checks, schedule verification, redeployment, and shared-key rotation.
