"""Azure DevOps work item and pull request enrichment for WorkBriefing."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from backend.auth.oauth import format_datetime, get_valid_access_token, utc_now
from backend.auth.pgshim import Connection


MICROSOFT_PROVIDER = "microsoft"
AZURE_DEVOPS_RESOURCE_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default offline_access openid profile"
ADO_API_VERSION = os.getenv("AZURE_DEVOPS_API_VERSION", "7.1")
CACHE_TTL = timedelta(minutes=int(os.getenv("CGA_ADO_TICKET_CACHE_MINUTES", "15")))

_PBI_TAG_RE = re.compile(r"\b(?:pbi|workitem|work-item|ado:pbi|ado:workitem)[:#-]?(\d{3,})\b", re.IGNORECASE)
_PR_TAG_RE = re.compile(r"\b(?:pr|pullrequest|pull-request|ado:pr)[:#-]?(\d{1,})\b", re.IGNORECASE)


@dataclass(frozen=True)
class ExternalRef:
    provider: str
    item_type: str
    item_id: str
    organization: str = ""
    project: str = ""
    repository: str = ""
    source: str = ""
    url: str = ""

    @property
    def cache_key(self) -> str:
        return "|".join(
            [
                self.provider,
                self.item_type,
                self.organization.lower(),
                self.project.lower(),
                self.repository.lower(),
                self.item_id,
            ]
        )


class AzureDevOpsEnricher:
    def __init__(
        self,
        *,
        db: Connection,
        user_id: int,
        client_id: str,
        token_url: str,
        scope: str = AZURE_DEVOPS_RESOURCE_SCOPE,
    ) -> None:
        self._db = db
        self._user_id = user_id
        self._client_id = client_id
        self._token_url = token_url
        self._scope = scope

    async def enrich_briefing(self, payload: dict[str, Any]) -> dict[str, Any]:
        activities = payload.get("activities")
        if not isinstance(activities, list) or not activities:
            return {**payload, "external_enrichment": {"status": "no_refs", "provider": "azure_devops"}}

        ref_map = self._collect_refs(activities)
        if not ref_map:
            return {**payload, "external_enrichment": {"status": "no_refs", "provider": "azure_devops"}}

        token_result = await get_valid_access_token(
            self._db,
            user_id=self._user_id,
            provider=MICROSOFT_PROVIDER,
            client_id=self._client_id,
            token_url=self._token_url,
            scope=self._scope,
        )
        if token_result.status != "ok" or not token_result.access_token:
            enriched_without_token = self._attach_unresolved_refs(activities, ref_map, token_result.status)
            return {
                **payload,
                "activities": enriched_without_token,
                "external_enrichment": {
                    "status": token_result.status,
                    "provider": "azure_devops",
                    "detail": token_result.detail,
                    "ref_count": len(ref_map),
                },
            }

        fetched: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=15.0) as client:
            for key, ref in ref_map.items():
                cached = await self._get_cached(key)
                if cached:
                    fetched[key] = cached
                    continue
                if not ref.organization:
                    fetched[key] = self._missing_context(ref, "organization is required")
                    continue
                if ref.item_type == "work_item":
                    details = await self._fetch_work_item(client, token_result.access_token, ref)
                elif ref.item_type == "pull_request":
                    details = await self._fetch_pull_request(client, token_result.access_token, ref)
                else:
                    details = self._missing_context(ref, "unsupported reference type")
                fetched[key] = details
                if details.get("status") == "ok":
                    await self._put_cached(key, ref, details)

        enriched_activities = self._attach_details(activities, ref_map, fetched)
        ok_count = sum(1 for item in fetched.values() if item.get("status") == "ok")
        return {
            **payload,
            "activities": enriched_activities,
            "external_enrichment": {
                "status": "ok",
                "provider": "azure_devops",
                "ref_count": len(ref_map),
                "resolved_count": ok_count,
                "expires_at": format_datetime(token_result.expires_at),
            },
        }

    def _collect_refs(self, activities: list[dict[str, Any]]) -> dict[str, ExternalRef]:
        refs: dict[str, ExternalRef] = {}
        for activity in activities:
            for ref in extract_azure_devops_refs(activity):
                refs.setdefault(ref.cache_key, ref)
        return refs

    def _attach_unresolved_refs(
        self,
        activities: list[dict[str, Any]],
        ref_map: dict[str, ExternalRef],
        status: str,
    ) -> list[dict[str, Any]]:
        details = {key: {**_ref_to_dict(ref), "status": status} for key, ref in ref_map.items()}
        return self._attach_details(activities, ref_map, details)

    def _attach_details(
        self,
        activities: list[dict[str, Any]],
        ref_map: dict[str, ExternalRef],
        details: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for activity in activities:
            refs = []
            for ref in extract_azure_devops_refs(activity):
                item = details.get(ref.cache_key) or {**_ref_to_dict(ref), "status": "not_resolved"}
                refs.append(item)
            if refs:
                metadata = _metadata(activity)
                metadata["external_refs"] = refs
                enriched.append({**activity, "raw_metadata": metadata, "external_refs": refs})
            else:
                enriched.append(activity)
        return enriched

    async def _fetch_work_item(self, client: httpx.AsyncClient, token: str, ref: ExternalRef) -> dict[str, Any]:
        url = f"https://dev.azure.com/{ref.organization}/_apis/wit/workitems/{ref.item_id}"
        params = {"api-version": ADO_API_VERSION, "$expand": "Fields"}
        response = await client.get(url, params=params, headers=_auth_headers(token))
        if response.status_code >= 400:
            return self._api_error(ref, response)
        data = response.json()
        fields = data.get("fields") if isinstance(data, dict) else {}
        fields = fields if isinstance(fields, dict) else {}
        assigned = fields.get("System.AssignedTo")
        if isinstance(assigned, dict):
            assigned_to = assigned.get("displayName") or assigned.get("uniqueName") or ""
        else:
            assigned_to = assigned or ""
        html_url = _nested(data, "_links", "html", "href") or ref.url or data.get("url") or ""
        return {
            **_ref_to_dict(ref),
            "status": "ok",
            "title": fields.get("System.Title") or "",
            "state": fields.get("System.State") or "",
            "work_item_type": fields.get("System.WorkItemType") or "",
            "assigned_to": assigned_to,
            "area_path": fields.get("System.AreaPath") or "",
            "iteration_path": fields.get("System.IterationPath") or "",
            "changed_date": fields.get("System.ChangedDate") or "",
            "url": html_url,
            "fetched_at": format_datetime(utc_now()),
        }

    async def _fetch_pull_request(self, client: httpx.AsyncClient, token: str, ref: ExternalRef) -> dict[str, Any]:
        if not (ref.project and ref.repository):
            return self._missing_context(ref, "project and repository are required for Azure DevOps PR lookup")
        repo = ref.repository
        url = f"https://dev.azure.com/{ref.organization}/{ref.project}/_apis/git/repositories/{repo}/pullRequests/{ref.item_id}"
        response = await client.get(url, params={"api-version": ADO_API_VERSION}, headers=_auth_headers(token))
        if response.status_code >= 400:
            return self._api_error(ref, response)
        data = response.json()
        created_by = data.get("createdBy") if isinstance(data, dict) else {}
        if not isinstance(created_by, dict):
            created_by = {}
        return {
            **_ref_to_dict(ref),
            "status": "ok",
            "title": data.get("title") or "",
            "state": data.get("status") or "",
            "created_by": created_by.get("displayName") or created_by.get("uniqueName") or "",
            "source_ref_name": data.get("sourceRefName") or "",
            "target_ref_name": data.get("targetRefName") or "",
            "url": ref.url or data.get("url") or "",
            "fetched_at": format_datetime(utc_now()),
        }

    async def _get_cached(self, cache_key: str) -> dict[str, Any] | None:
        async with self._db.execute(
            """
            SELECT details_json, expires_at
            FROM external_ticket_cache
            WHERE cache_key = ? AND provider = 'azure_devops'
            """,
            (cache_key,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        expires_at = row["expires_at"]
        if expires_at and str(expires_at) <= (format_datetime(utc_now()) or ""):
            return None
        try:
            data = json.loads(row["details_json"] or "{}")
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            data["cache_hit"] = True
            return data
        return None

    async def _put_cached(self, cache_key: str, ref: ExternalRef, details: dict[str, Any]) -> None:
        now = utc_now()
        expires_at = now + CACHE_TTL
        await self._db.execute(
            """
            INSERT INTO external_ticket_cache(
                cache_key, provider, item_type, organization, project, repository,
                item_id, details_json, fetched_at, expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
                details_json = EXCLUDED.details_json,
                fetched_at = EXCLUDED.fetched_at,
                expires_at = EXCLUDED.expires_at
            """,
            (
                cache_key,
                "azure_devops",
                ref.item_type,
                ref.organization,
                ref.project,
                ref.repository,
                ref.item_id,
                json.dumps(details, sort_keys=True, ensure_ascii=True),
                format_datetime(now),
                format_datetime(expires_at),
            ),
        )
        await self._db.commit()

    @staticmethod
    def _missing_context(ref: ExternalRef, reason: str) -> dict[str, Any]:
        return {**_ref_to_dict(ref), "status": "missing_context", "detail": reason}

    @staticmethod
    def _api_error(ref: ExternalRef, response: httpx.Response) -> dict[str, Any]:
        detail = response.text[:500]
        try:
            data = response.json()
            if isinstance(data, dict):
                detail = str(data.get("message") or data.get("error_description") or detail)
        except Exception:
            pass
        return {**_ref_to_dict(ref), "status": "api_error", "status_code": response.status_code, "detail": detail}


def extract_azure_devops_refs(activity: dict[str, Any]) -> list[ExternalRef]:
    metadata = _metadata(activity)
    refs: list[ExternalRef] = []
    source_url = str(activity.get("source_url") or metadata.get("url") or "")
    url_context = _parse_ado_url(source_url)

    ado_meta = metadata.get("ado") if isinstance(metadata.get("ado"), dict) else {}
    organization = str(ado_meta.get("organization") or metadata.get("organization") or url_context.get("organization") or "")
    project = str(ado_meta.get("project") or metadata.get("project") or url_context.get("project") or "")

    work_item_id = _first_value(
        ado_meta.get("work_item_id"),
        ado_meta.get("pbi_id"),
        metadata.get("work_item_id"),
        metadata.get("pbi_id"),
        url_context.get("work_item_id"),
    )
    if work_item_id:
        refs.append(
            ExternalRef(
                provider="azure_devops",
                item_type="work_item",
                item_id=work_item_id,
                organization=organization,
                project=project,
                source="metadata_or_url",
                url=source_url,
            )
        )

    text = " ".join(
        [
            str(activity.get("source_item_id") or ""),
            str(activity.get("external_id") or ""),
            str(activity.get("title") or ""),
            str(activity.get("summary") or ""),
            " ".join(str(tag) for tag in activity.get("tags") or []),
        ]
    )
    for match in _PBI_TAG_RE.finditer(text):
        refs.append(
            ExternalRef(
                provider="azure_devops",
                item_type="work_item",
                item_id=match.group(1),
                organization=organization,
                project=project,
                source="text",
                url=source_url,
            )
        )

    pr_context = _pr_context_from_metadata(metadata, url_context)
    if pr_context.get("item_id"):
        refs.append(
            ExternalRef(
                provider="azure_devops",
                item_type="pull_request",
                item_id=str(pr_context.get("item_id")),
                organization=str(pr_context.get("organization") or organization),
                project=str(pr_context.get("project") or project),
                repository=str(pr_context.get("repository") or ""),
                source="metadata_or_url",
                url=str(pr_context.get("url") or source_url),
            )
        )
    for match in _PR_TAG_RE.finditer(text):
        refs.append(
            ExternalRef(
                provider="azure_devops",
                item_type="pull_request",
                item_id=match.group(1),
                organization=organization,
                project=project,
                repository=str(pr_context.get("repository") or ""),
                source="text",
                url=source_url,
            )
        )

    unique: dict[str, ExternalRef] = {}
    for ref in refs:
        if ref.item_id:
            unique.setdefault(ref.cache_key, ref)
    return list(unique.values())


def _metadata(activity: dict[str, Any]) -> dict[str, Any]:
    raw = activity.get("raw_metadata") or activity.get("metadata") or {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_ado_url(value: str) -> dict[str, str]:
    if not value:
        return {}
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    path = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    context: dict[str, str] = {}
    if host == "dev.azure.com" and path:
        context["organization"] = path[0]
        if len(path) > 1:
            context["project"] = path[1]
    elif host.endswith(".visualstudio.com"):
        context["organization"] = host.split(".")[0]
        if path:
            context["project"] = path[0]

    match = re.search(r"/_workitems/edit/(\d+)", parsed.path, re.IGNORECASE)
    if match:
        context["work_item_id"] = match.group(1)
    pr_match = re.search(r"/_git/([^/]+)/pullrequest/(\d+)", parsed.path, re.IGNORECASE)
    if pr_match:
        context["repository"] = unquote(pr_match.group(1))
        context["pull_request_id"] = pr_match.group(2)
    return context


def _pr_context_from_metadata(metadata: dict[str, Any], url_context: dict[str, str]) -> dict[str, Any]:
    pr_meta = metadata.get("pr") if isinstance(metadata.get("pr"), dict) else {}
    ado_pr = metadata.get("ado_pr") if isinstance(metadata.get("ado_pr"), dict) else {}
    pull_requests = metadata.get("pull_requests") if isinstance(metadata.get("pull_requests"), list) else []
    first_pr = pull_requests[0] if pull_requests and isinstance(pull_requests[0], dict) else {}
    context = {
        "organization": ado_pr.get("organization") or first_pr.get("organization") or url_context.get("organization"),
        "project": ado_pr.get("project") or first_pr.get("project") or url_context.get("project"),
        "repository": ado_pr.get("repository") or first_pr.get("repository") or url_context.get("repository"),
        "item_id": _first_value(
            ado_pr.get("pull_request_id"),
            ado_pr.get("pullRequestId"),
            first_pr.get("pullRequestId"),
            first_pr.get("id"),
            url_context.get("pull_request_id"),
        ),
        "url": ado_pr.get("url") or first_pr.get("url"),
    }
    if not context["item_id"] and isinstance(pr_meta, dict):
        pr_url = str(pr_meta.get("url") or "")
        parsed = _parse_ado_url(pr_url)
        if parsed.get("pull_request_id"):
            context.update(
                {
                    "organization": context["organization"] or parsed.get("organization"),
                    "project": context["project"] or parsed.get("project"),
                    "repository": context["repository"] or parsed.get("repository"),
                    "item_id": parsed.get("pull_request_id"),
                    "url": pr_url,
                }
            )
    return context


def _ref_to_dict(ref: ExternalRef) -> dict[str, Any]:
    return {
        "provider": ref.provider,
        "type": ref.item_type,
        "id": ref.item_id,
        "organization": ref.organization,
        "project": ref.project,
        "repository": ref.repository,
        "source": ref.source,
        "url": ref.url,
    }


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _first_value(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _nested(data: Any, *keys: str) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current