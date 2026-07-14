# Azure Policy Change Monitor Operations

This runbook covers production setup, least-privilege access, rollout, health checks, and recovery for the CGA Azure Policy Change Monitor.

## Production Preconditions

- CGA database migrations have created extension configuration, run, and snapshot tables.
- The `schedule_worker` runtime module is enabled on exactly the intended worker fleet.
- The CGA host can reach the selected Azure Management and Microsoft Entra endpoints over HTTPS.
- The runtime identity exists and has only the collection permissions listed below.
- Secret environment variables are injected by the hosting platform, not stored in CGA configuration.
- Database backup and restore have been tested before relying on snapshot history for incident evidence.

## Least-Privilege RBAC

The monitor never writes Azure resources. A custom read-only role can grant only the operations used by the adapter:

| Operation | Why |
| --- | --- |
| `Microsoft.Authorization/policyDefinitions/read` | List policy definitions. |
| `Microsoft.Authorization/policySetDefinitions/read` | List initiatives. |
| `Microsoft.Authorization/policyAssignments/read` | List assignments. |
| `Microsoft.PolicyInsights/policyStates/queryResults/action` | Query latest compliance state when enabled. |
| `Microsoft.Insights/eventtypes/values/read` | Read Activity Log events when enabled. |

Assign inventory and Policy Insights permissions at the monitored subscription or management group. Activity Log is subscription-scoped: for a management-group monitor, grant Activity Log read access on every subscription named in `activity_subscription_ids`.

The built-in **Reader** role covers Azure resource read operations and is a practical starting point, but verify that the effective role in the target cloud permits the Policy Insights query action. If it does not, add only `Microsoft.PolicyInsights/policyStates/queryResults/action` through an approved custom read-only role. Do not use Owner, Contributor, or Resource Policy Contributor merely to run this monitor.

If compliance or Activity Log collection is disabled, omit its corresponding permission.

## Identity Setup

### Managed Identity

1. Enable a system-assigned identity on the CGA host or attach a user-assigned identity.
2. Assign the read-only role at each monitored scope.
3. For a user-assigned identity, set `managed_identity_client_id`.
4. Set `auth_mode=managed_identity`.

Managed identity is the preferred Azure-hosted configuration because no renewable credential enters the application environment.

### Workload Identity

Provide `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `AZURE_FEDERATED_TOKEN_FILE`, then set `auth_mode=workload_identity`. The token file is read for each token request and its assertion is never persisted.

### Environment Credential

Provide `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and exactly one of `AZURE_FEDERATED_TOKEN_FILE` or `AZURE_CLIENT_SECRET`. Federation is preferred. Keep any client secret in the platform secret store and rotate it independently of CGA.

### Azure CLI

Use `auth_mode=azure_cli` only for local smoke tests. The executing OS account must have an active Azure CLI session for the same tenant and cloud. Do not depend on an interactive CLI session in a scheduled production service.

## Sovereign Clouds

Update `management_endpoint`, `authority_host`, and `arm_token_scope` as one unit. For model summaries, also use the cloud-specific Azure OpenAI endpoint and audience:

| Cloud | Azure OpenAI DNS suffix | Typical model token scope |
| --- | --- | --- |
| Public | `openai.azure.com` | `https://cognitiveservices.azure.com/.default` |
| US Government | `openai.azure.us` | `https://cognitiveservices.azure.us/.default` |
| China | `openai.azure.cn` | `https://cognitiveservices.azure.cn/.default` |

Confirm endpoint and model availability in the target region before enabling summaries. A model outage does not stop deterministic monitoring.

## Secret Injection

| Secret | Configuration contains | Runtime environment contains |
| --- | --- | --- |
| Azure client secret, when unavoidable | Nothing | `AZURE_CLIENT_SECRET` |
| Federated assertion | File path only | `AZURE_FEDERATED_TOKEN_FILE` and mounted token file |
| Model API key | Variable name in `model_api_key_env` | The named variable, commonly `AZURE_POLICY_MONITOR_MODEL_API_KEY` |
| Webhook URL | Variable name in `notification_webhook_env` | The named variable |
| SMTP password | Nothing | `CGA_SMTP_PASSWORD` |

Never put a secret in a run override, endpoint query string, webhook configuration value, schedule payload, or repository file. Extension configuration validation rejects recognized secret keys recursively.

## Rollout Procedure

1. Configure repository collection first and run it once if repository evidence is required.
2. Enable Azure collection for one non-production subscription with compliance and Activity Log enabled.
3. Run once and confirm `summary.azure.baseline_created=true`. This establishes the scope baseline.
4. Run a second time without changes and confirm zero Azure drift findings.
5. Make or identify an approved policy change, run again, and verify the deterministic finding and evidence.
6. Enable optional summary output and confirm a provider failure leaves the extension run successful.
7. Enable one notification channel at a `high` threshold and exercise it with controlled evidence.
8. Add production scopes gradually, then create schedules.

For management groups, enumerate all child subscriptions whose Activity Logs are required. The monitor intentionally fails validation rather than silently claiming management-group activity coverage with an empty list.

## Scheduling and Retention

- Use a cadence appropriate to the change-detection objective; 15-60 minutes is a normal starting range.
- Set `activity_lookback_minutes` to at least twice the cadence during initial rollout.
- Keep `snapshot_retention_count` large enough for the investigation horizon. The default of 90 snapshots represents 45 hours at a 30-minute cadence or 90 days at a daily cadence.
- Scope changes create a new independent history. They do not delete snapshots for the old scope.
- Run and snapshot retention are separate concerns; include the CGA database in backup policy.

## Health Checks

For each scheduled scope, monitor:

- Latest extension run exists within two schedule intervals.
- Run `status` is `success` and `monitoring.read_only` is `true`.
- Azure summary counts are nonzero when the estate is known to contain policy objects.
- Snapshot timestamp advances after each successful collection.
- Notification status is `sent`, `below_threshold`, or `disabled`; investigate `partial` and `failed`.
- Summary status is `generated`, `not_needed`, or `disabled`; `failed` affects presentation only.

Treat a successful baseline with unexpectedly empty inventory as an access or scope incident, not as proof that no policies exist.

## Failure Guide

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `AzureTokenError` | Missing identity inputs, wrong tenant/cloud, expired CLI session, or unavailable managed identity endpoint | Verify explicit auth mode and the runtime environment; do not add credentials to extension config. |
| `AzureRestError` with 401/403 | Token audience or RBAC mismatch | Check all three cloud endpoint fields and effective permissions at the exact scope. |
| Management-group Activity Log validation error | No `activity_subscription_ids` | Add every required subscription and grant each one Activity Log read access. |
| Collection-limit error | Scope returns more than `max_collection_items` or pagination exceeds the hard cap | Narrow the scope or raise the bounded item limit after capacity review. |
| Repeated 429/5xx failure | Azure throttling or service incident exceeded bounded retries | Increase cadence, reduce scope fan-out, and retry after Azure recovers. Do not make retries unbounded. |
| Summary `failed` | Model endpoint, auth, response shape, or content filter issue | Inspect `error_type`, provider telemetry, endpoint/deployment, and environment variable presence. Deterministic findings remain valid. |
| Notification `failed` or `partial` | Webhook, SMTP, DNS, TLS, or credential issue | Test the failing channel independently and verify runtime secret injection. Do not paste the URL or password into CGA. |
| No findings on first run | Expected baseline behavior | Run again after the baseline exists. |
| Unexpected large drift | Scope changed, approved bulk rollout, or stale/missing baseline | Confirm the normalized scope and compare run timestamps before declaring an incident. |

Persisted extension execution failures intentionally contain only an error type. Use service logs and provider telemetry for detailed diagnosis; this prevents credential-bearing exception text from entering database history.

## Incident Response

1. Preserve the relevant run record and its preceding/current snapshots through the normal CGA database backup process.
2. Confirm the finding is deterministic; optional prose is supporting context only.
3. Correlate the resource ID, content hashes, assignment fields, compliance delta, Activity Log event ID, caller, and correlation ID.
4. Validate the change through the approved Azure change record.
5. Remediate through normal Azure Policy deployment tooling. The monitor cannot and must not mutate Azure.
6. Run the monitor again to capture the post-remediation state.

## Recovery

- A failed Azure collection does not replace the last good snapshot.
- A failed model or notification provider does not block snapshot persistence.
- Restarting CGA does not reset baselines because snapshots are database-backed.
- Restoring the CGA database restores run and snapshot history together; verify the next run uses the expected normalized scope.
- To intentionally establish a new baseline, use a new scope or remove the relevant snapshots through an approved database maintenance procedure. Never delete history as a first response to unexplained drift.