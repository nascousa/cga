"""Auth API router: login, users, projects, tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from jose import jwt as jose_jwt

from backend import runtime_config
from backend.auth.database import get_db
from backend.auth.dependencies import get_current_user, require_admin, get_consumer, get_registry
from backend.auth import pgshim as aiosqlite  # type: ignore  # asyncpg shim providing aiosqlite-compatible API
from backend.auth.models import (
    AuditLogOut,
    AdminUserUpdate,
    GenerateTokenRequest,
    GraphLiveStats,
    IndexJobStatus,
    LoginRequest,
    ProjectCreate,
    ProjectIndexRecoveryOut,
    ProjectIndexTriggerOut,
    ProjectIndexStatus,
    ProjectOut,
    ProjectTokenOut,
    ProjectUpdate,
    TokenResponse,
    UserCreate,
    PaginatedAuditOut,
    UserOut,
    UserProfileUpdate,
)
from backend.auth.oauth import (
    deactivate_oauth_connection,
    get_oauth_connection_status,
    normalize_token_response,
    upsert_oauth_connection,
)
from backend.tools import server as mcp_server
from backend.auth.security import (
    create_access_token,
    generate_project_token as generate_project_token_value,
    generate_token,
    hash_password,
    hash_token,
    token_hint,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID", "").strip()
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "").strip()
GITHUB_OAUTH_CALLBACK_URL = os.getenv("GITHUB_OAUTH_CALLBACK_URL", "").strip()
GITHUB_OAUTH_SCOPE = os.getenv("GITHUB_OAUTH_SCOPE", "read:user user:email").strip() or "read:user user:email"
GITHUB_OAUTH_DEFAULT_ROLE = os.getenv("GITHUB_OAUTH_DEFAULT_ROLE", "developer").strip().lower()
GITHUB_OAUTH_ALLOWED_USERS = {
    v.strip().lower() for v in os.getenv("GITHUB_OAUTH_ALLOWED_USERS", "").split(",") if v.strip()
}
GITHUB_OAUTH_ALLOWED_ORGS = {
    v.strip().lower() for v in os.getenv("GITHUB_OAUTH_ALLOWED_ORGS", "").split(",") if v.strip()
}
GITHUB_OAUTH_STATE_SECRET = os.getenv("GITHUB_OAUTH_STATE_SECRET", os.getenv("JWT_SECRET_KEY", "dev-local-state-secret"))
GITHUB_OAUTH_STATE_TTL_SEC = int(os.getenv("GITHUB_OAUTH_STATE_TTL_SEC", "600"))
INDEX_STALE_AFTER_SEC = int(os.getenv("CONTEXTGRAPH_INDEX_STALE_AFTER_SEC", "900"))
_LOCAL_REPOS_ROOT = Path(__file__).resolve().parents[4]

MICROSOFT_OAUTH_TENANT = os.getenv("MICROSOFT_OAUTH_TENANT", "organizations").strip() or "organizations"
MICROSOFT_OAUTH_CLIENT_ID = (
    os.getenv("MICROSOFT_OAUTH_CLIENT_ID")
    or os.getenv("CGA_MICROSOFT_CLIENT_ID")
    or os.getenv("AZURE_DEVOPS_OAUTH_CLIENT_ID")
    or "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
).strip()
MICROSOFT_OAUTH_SCOPE = (
    os.getenv("MICROSOFT_OAUTH_SCOPE")
    or "499b84ac-1321-427f-aa17-267ca6975798/.default offline_access openid profile"
).strip()
MICROSOFT_OAUTH_PROVIDER = "microsoft"
MICROSOFT_DEVICE_CODE_URL = f"https://login.microsoftonline.com/{MICROSOFT_OAUTH_TENANT}/oauth2/v2.0/devicecode"
MICROSOFT_TOKEN_URL = f"https://login.microsoftonline.com/{MICROSOFT_OAUTH_TENANT}/oauth2/v2.0/token"
_PENDING_MICROSOFT_DEVICE_CODES: dict[str, dict] = {}


def _repo_name_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _repo_search_roots() -> list[Path]:
    return runtime_config.get_indexing_repo_search_roots([
        _LOCAL_REPOS_ROOT,
        Path("/repos"),
        Path("D:/Repos"),
        Path("d:/repos"),
    ])


def _candidate_repo_paths(project_name: str) -> list[str]:
    candidates: list[str] = []
    normalized = project_name.strip()
    if not normalized:
        return candidates

    normalized_key = _repo_name_key(normalized)

    for root in _repo_search_roots():
        try:
            if root.exists():
                for child in root.iterdir():
                    if child.is_dir() and _repo_name_key(child.name) == normalized_key:
                        candidates.append(str(child))
        except Exception:
            pass

    fallbacks = []
    for root in _repo_search_roots():
        fallbacks.append(str(root / normalized))
        fallbacks.append(str(root / normalized.lower()))
    seen = {c.lower() for c in candidates}
    for candidate in fallbacks:
        if candidate.lower() not in seen:
            candidates.append(candidate)
            seen.add(candidate.lower())
    return candidates


def _resolve_repo_path(project_name: str) -> str | None:
    for candidate in _candidate_repo_paths(project_name):
        try:
            if Path(candidate).exists():
                return candidate
        except Exception:
            continue
    return None


def _resolve_project_repo_path(project: dict) -> str | None:
    """Resolve repo path for a project, preferring the stored repo_path column.

    Falls back to name-based directory scanning for backward compatibility
    with projects created before the repo_path column was added.
    """
    stored = (project.get("repo_path") or "").strip()
    if stored:
        try:
            if Path(stored).exists():
                return stored
        except Exception:
            pass
    return _resolve_repo_path(project["project_name"])


def _project_repo_status_paths(project: dict) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    stored = (project.get("repo_path") or "").strip()
    if stored:
        candidates.append(stored)
        seen.add(stored.lower())

    for candidate in _candidate_repo_paths(project["project_name"]):
        key = candidate.lower()
        if key in seen:
            continue
        candidates.append(candidate)
        seen.add(key)

    return candidates


def _github_oauth_enabled() -> bool:
    return bool(GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET and GITHUB_OAUTH_CALLBACK_URL)


def _state_b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _state_b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - (len(value) % 4)) % 4)
    return base64.urlsafe_b64decode((value + padding).encode())


def _create_state() -> str:
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    payload = f"{ts}.{nonce}"
    sig = hmac.new(GITHUB_OAUTH_STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return _state_b64url_encode(f"{payload}.{sig}".encode())


def _validate_state(state: str) -> bool:
    try:
        raw = _state_b64url_decode(state).decode()
        ts_s, nonce, sig = raw.split(".", 2)
        payload = f"{ts_s}.{nonce}"
        expected = hmac.new(GITHUB_OAUTH_STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        issued = int(ts_s)
        return (int(time.time()) - issued) <= GITHUB_OAUTH_STATE_TTL_SEC
    except Exception:
        return False


async def _github_in_allowed_orgs(token: str) -> bool:
    if not GITHUB_OAUTH_ALLOWED_ORGS:
        return True
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            "https://api.github.com/user/orgs",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if r.status_code >= 400:
        return False
    orgs = r.json() if isinstance(r.json(), list) else []
    user_orgs = {str(item.get("login", "")).lower() for item in orgs}
    return bool(user_orgs.intersection(GITHUB_OAUTH_ALLOWED_ORGS))


def _safe_username(base: str) -> str:
    text = "".join(ch for ch in base if ch.isalnum() or ch in "._-")[:56].strip("._-")
    if len(text) < 3:
        text = f"gh_{secrets.token_hex(3)}"
    return text


async def _ensure_unique_username(db: aiosqlite.Connection, preferred: str) -> str:
    username = _safe_username(preferred)
    for idx in range(50):
        candidate = username if idx == 0 else f"{username}_{idx}"
        async with db.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)) as cur:
            if not await cur.fetchone():
                return candidate
    return f"gh_{secrets.token_hex(5)}"


def _oauth_error_redirect(msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"/admin?oauth_error={msg}", status_code=302)


def _oauth_success_page(token: str) -> HTMLResponse:
    script_token = token.replace("\\", "\\\\").replace("'", "\\'")
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><title>CG Admin OAuth</title></head>"
        "<body><script>"
        f"localStorage.setItem('cg_jwt','{script_token}');"
        "window.location='/admin';"
        "</script></body></html>"
    )
    return HTMLResponse(content=html, status_code=200)


def _microsoft_oauth_enabled() -> bool:
    return bool(MICROSOFT_OAUTH_CLIENT_ID and MICROSOFT_OAUTH_SCOPE)


def _cleanup_pending_microsoft_codes() -> None:
    now = time.time()
    expired = [key for key, value in _PENDING_MICROSOFT_DEVICE_CODES.items() if float(value.get("expires_at", 0)) <= now]
    for key in expired:
        _PENDING_MICROSOFT_DEVICE_CODES.pop(key, None)


def _claims_from_jwt(token: str | None) -> dict:
    if not token:
        return {}
    try:
        claims = jose_jwt.get_unverified_claims(token)
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _microsoft_account_from_token(token_cache: dict, fallback_user: dict) -> tuple[str, str]:
    claims = _claims_from_jwt(token_cache.get("id_token"))
    account_id = str(
        claims.get("oid")
        or claims.get("sub")
        or claims.get("preferred_username")
        or fallback_user.get("id")
        or "default"
    )
    display_name = str(
        claims.get("name")
        or claims.get("preferred_username")
        or fallback_user.get("email")
        or fallback_user.get("username")
        or account_id
    )
    return account_id, display_name


def _device_code_error_detail(response: httpx.Response) -> tuple[str, str]:
    try:
        data = response.json()
    except Exception:
        return "http_error", response.text[:500]
    if not isinstance(data, dict):
        return "http_error", str(response.status_code)
    return str(data.get("error") or "http_error"), str(data.get("error_description") or data.get("message") or "")


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _estimate_processing_remaining(job_data: dict, avg_duration_sec: int) -> int:
    started_at = _parse_iso_datetime(job_data.get("updated_at"))
    if not started_at:
        return avg_duration_sec
    elapsed = max(0, int((datetime.now(started_at.tzinfo) - started_at).total_seconds()))
    return max(1, avg_duration_sec - elapsed)


def _job_age_seconds(job_data: dict) -> int | None:
    updated_at = _parse_iso_datetime(job_data.get("updated_at"))
    if not updated_at:
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds()))


def _build_index_job_status(
    job_data: dict,
    pending_by_id: dict[str, int],
    avg_duration_sec: int,
    processing_remaining: int,
) -> IndexJobStatus:
    queue_position = None
    eta_seconds = None
    job_id = str(job_data.get("job_id", ""))
    job_status = job_data.get("status", "")
    age_seconds = _job_age_seconds(job_data)
    is_stale = bool(
        job_status == "processing"
        and age_seconds is not None
        and age_seconds >= INDEX_STALE_AFTER_SEC
    )

    if is_stale:
        job_status = "stale"
    elif job_status == "pending":
        queue_position = pending_by_id.get(job_id)
        if queue_position:
            eta_seconds = int(queue_position * avg_duration_sec + processing_remaining)
    elif job_status == "processing":
        queue_position = 0
        eta_seconds = _estimate_processing_remaining(job_data, avg_duration_sec)

    return IndexJobStatus(
        job_id=job_id,
        job_type=job_data.get("job_type", ""),
        repo_path=job_data.get("repo_path", ""),
        status=job_status,
        created_at=job_data.get("created_at", ""),
        updated_at=job_data.get("updated_at", ""),
        error=job_data.get("error") or ("No status update within stale threshold" if is_stale else None),
        files=int(job_data["files"]) if "files" in job_data and job_data["files"] else None,
        symbols=int(job_data["symbols"]) if "symbols" in job_data and job_data["symbols"] else None,
        queue_position=queue_position,
        eta_seconds=eta_seconds,
        is_stale=is_stale,
        age_seconds=age_seconds,
    )


async def _get_active_project(project_id: int, db: aiosqlite.Connection) -> dict:
    async with db.execute(
        "SELECT id, project_name, project_id, repo_path, is_active FROM projects WHERE id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    project = dict(row)
    if not project.get("is_active"):
        raise HTTPException(status_code=400, detail="Project is inactive")
    return project


# ── Helper ─────────────────────────────────────────────────────────────────

def _random_project_id(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _query_graph_live_stats(registry, project_name: str) -> GraphLiveStats | None:
    """Query FalkorDB for live node/edge counts of a project graph."""
    if not registry:
        return None
    try:
        graph = registry.get(project_name)
        def _q(cypher: str):
            return (graph.query(cypher).result_set or [[0]])[0][0]

        files = _q("MATCH (f:File) RETURN count(f)")
        symbols = _q("MATCH (s:Symbol) RETURN count(s)")
        variables = _q("MATCH (v:Variable) RETURN count(v)")
        repos = _q("MATCH (r:Repository) RETURN count(r)")
        call_edges = _q("MATCH ()-[c:CALLS]->() RETURN count(c)")
        flow_edges = _q("MATCH ()-[f:FLOWS_TO]->() RETURN count(f)")
        uses_var = _q("MATCH ()-[u:USES_VARIABLE]->() RETURN count(u)")
        defines = _q("MATCH ()-[d:DEFINES]->() RETURN count(d)")
        contains = _q("MATCH ()-[c:CONTAINS]->() RETURN count(c)")
        total_nodes = files + symbols + variables + repos
        total_edges = call_edges + flow_edges + uses_var + defines + contains
        return GraphLiveStats(
            files=files,
            symbols=symbols,
            variables=variables,
            call_edges=call_edges,
            flow_edges=flow_edges,
            uses_variable_edges=uses_var,
            defines_edges=defines,
            contains_edges=contains,
            total_nodes=total_nodes,
            total_edges=total_edges,
        )
    except Exception:
        return None


# ── Auth ───────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute(
        "SELECT password_hash, role, is_active FROM users WHERE username = ?",
        (body.username,),
    ) as cur:
        row = await cur.fetchone()

    if not row or not row["is_active"] or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token({"sub": body.username, "role": row["role"]})
    return TokenResponse(access_token=token)


@router.get("/github/start")
async def github_oauth_start():
    if not _github_oauth_enabled():
        raise HTTPException(status_code=400, detail="GitHub OAuth is not configured")
    state = _create_state()
    params = {
        "client_id": GITHUB_OAUTH_CLIENT_ID,
        "redirect_uri": GITHUB_OAUTH_CALLBACK_URL,
        "scope": GITHUB_OAUTH_SCOPE,
        "state": state,
    }
    return RedirectResponse(url=f"https://github.com/login/oauth/authorize?{urlencode(params)}", status_code=302)


@router.get("/github/callback")
async def github_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
):
    if error:
        return _oauth_error_redirect("github_oauth_denied")
    if not _github_oauth_enabled():
        return _oauth_error_redirect("github_oauth_not_configured")
    if not code or not state:
        return _oauth_error_redirect("github_oauth_bad_request")
    if not _validate_state(state):
        return _oauth_error_redirect("github_oauth_invalid_state")

    async with httpx.AsyncClient(timeout=20.0) as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_OAUTH_CLIENT_ID,
                "client_secret": GITHUB_OAUTH_CLIENT_SECRET,
                "code": code,
                "redirect_uri": GITHUB_OAUTH_CALLBACK_URL,
                "state": state,
            },
        )

    if token_resp.status_code >= 400:
        return _oauth_error_redirect("github_oauth_exchange_failed")

    token_data = token_resp.json()
    gh_access_token = token_data.get("access_token")
    if not gh_access_token:
        return _oauth_error_redirect("github_oauth_no_token")

    headers = {
        "Authorization": f"Bearer {gh_access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        user_resp = await client.get("https://api.github.com/user", headers=headers)
    if user_resp.status_code >= 400:
        return _oauth_error_redirect("github_oauth_user_fetch_failed")
    gh_user = user_resp.json()

    gh_id = str(gh_user.get("id", "")).strip()
    gh_login = str(gh_user.get("login", "")).strip()
    gh_email = str(gh_user.get("email") or "").strip().lower()
    if not gh_id or not gh_login:
        return _oauth_error_redirect("github_oauth_missing_user")

    if not gh_email:
        async with httpx.AsyncClient(timeout=20.0) as client:
            email_resp = await client.get("https://api.github.com/user/emails", headers=headers)
        if email_resp.status_code < 400:
            emails = email_resp.json() if isinstance(email_resp.json(), list) else []
            primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
            any_verified = next((e for e in emails if e.get("verified")), None)
            candidate = primary or any_verified
            if candidate:
                gh_email = str(candidate.get("email") or "").strip().lower()

    if GITHUB_OAUTH_ALLOWED_USERS and gh_login.lower() not in GITHUB_OAUTH_ALLOWED_USERS:
        return _oauth_error_redirect("github_oauth_user_not_allowed")

    if not await _github_in_allowed_orgs(gh_access_token):
        return _oauth_error_redirect("github_oauth_org_not_allowed")

    async with db.execute(
        "SELECT id, username, role, is_active FROM users WHERE github_id = ?",
        (gh_id,),
    ) as cur:
        user_row = await cur.fetchone()

    if not user_row and gh_email:
        async with db.execute(
            "SELECT id, username, role, is_active FROM users WHERE LOWER(email) = ?",
            (gh_email,),
        ) as cur:
            email_row = await cur.fetchone()
        if email_row:
            await db.execute(
                "UPDATE users SET github_id = ?, auth_provider = 'github' WHERE id = ?",
                (gh_id, email_row["id"]),
            )
            await db.commit()
            user_row = email_row

    if not user_row:
        role = GITHUB_OAUTH_DEFAULT_ROLE if GITHUB_OAUTH_DEFAULT_ROLE in {"admin", "developer"} else "developer"
        username = await _ensure_unique_username(db, gh_login)
        password_hash = hash_password(generate_token())
        try:
            async with db.execute(
                """
                INSERT INTO users(username, password_hash, role, email, auth_provider, github_id, is_active)
                VALUES(?,?,?,?,?,?,1)
                RETURNING id, username, role, is_active
                """,
                (username, password_hash, role, gh_email, "github", gh_id),
            ) as cur:
                user_row = await cur.fetchone()
            await db.commit()
        except aiosqlite.IntegrityError:
            return _oauth_error_redirect("github_oauth_conflict")

    if not user_row or not bool(user_row["is_active"]):
        return _oauth_error_redirect("github_oauth_user_inactive")

    app_token = create_access_token({"sub": user_row["username"], "role": user_row["role"]})
    return _oauth_success_page(app_token)


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return UserOut(
        id=user["id"],
        username=user["username"],
        email=user.get("email", "") or "",
        role=user["role"],
        created_at="",
        is_active=bool(user["is_active"]),
    )


@router.get("/microsoft/status")
async def microsoft_connection_status(
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    status_payload = await get_oauth_connection_status(
        db,
        user_id=int(user["id"]),
        provider=MICROSOFT_OAUTH_PROVIDER,
    )
    return {
        "configured": _microsoft_oauth_enabled(),
        "client_id": MICROSOFT_OAUTH_CLIENT_ID,
        "tenant": MICROSOFT_OAUTH_TENANT,
        "scope": MICROSOFT_OAUTH_SCOPE,
        **status_payload,
    }


@router.post("/microsoft/device/start")
async def microsoft_device_start(user: dict = Depends(get_current_user)) -> dict:
    if not _microsoft_oauth_enabled():
        raise HTTPException(status_code=400, detail="Microsoft OAuth is not configured")
    _cleanup_pending_microsoft_codes()
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            MICROSOFT_DEVICE_CODE_URL,
            data={"client_id": MICROSOFT_OAUTH_CLIENT_ID, "scope": MICROSOFT_OAUTH_SCOPE},
        )
    if response.status_code >= 400:
        error, detail = _device_code_error_detail(response)
        raise HTTPException(status_code=400, detail=f"{error}: {detail}".strip())

    data = response.json()
    device_code = str(data.get("device_code") or "")
    if not device_code:
        raise HTTPException(status_code=400, detail="Microsoft device flow did not return a device code")

    poll_id = secrets.token_urlsafe(24)
    expires_in = int(data.get("expires_in") or 900)
    interval = max(2, int(data.get("interval") or 5))
    _PENDING_MICROSOFT_DEVICE_CODES[poll_id] = {
        "user_id": int(user["id"]),
        "device_code": device_code,
        "expires_at": time.time() + expires_in,
        "interval": interval,
    }
    return {
        "poll_id": poll_id,
        "user_code": data.get("user_code"),
        "verification_uri": data.get("verification_uri"),
        "verification_uri_complete": data.get("verification_uri_complete"),
        "message": data.get("message"),
        "expires_in": expires_in,
        "interval": interval,
    }


@router.post("/microsoft/device/poll")
async def microsoft_device_poll(
    payload: dict,
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    poll_id = str((payload or {}).get("poll_id") or "")
    pending = _PENDING_MICROSOFT_DEVICE_CODES.get(poll_id)
    if not pending or int(pending.get("user_id") or 0) != int(user["id"]):
        raise HTTPException(status_code=404, detail="Microsoft device login session not found")
    if float(pending.get("expires_at") or 0) <= time.time():
        _PENDING_MICROSOFT_DEVICE_CODES.pop(poll_id, None)
        raise HTTPException(status_code=410, detail="Microsoft device login session expired")

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            MICROSOFT_TOKEN_URL,
            data={
                "client_id": MICROSOFT_OAUTH_CLIENT_ID,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": pending["device_code"],
            },
        )
    if response.status_code >= 400:
        error, detail = _device_code_error_detail(response)
        if error in {"authorization_pending", "slow_down"}:
            return {
                "status": "pending",
                "detail": detail or error,
                "interval": int(pending.get("interval") or 5) + (5 if error == "slow_down" else 0),
            }
        if error in {"authorization_declined", "expired_token", "bad_verification_code"}:
            _PENDING_MICROSOFT_DEVICE_CODES.pop(poll_id, None)
        raise HTTPException(status_code=400, detail=f"{error}: {detail}".strip())

    token_data = response.json()
    token_cache = normalize_token_response(token_data)
    account_id, display_name = _microsoft_account_from_token(token_cache, user)
    await upsert_oauth_connection(
        db,
        user_id=int(user["id"]),
        provider=MICROSOFT_OAUTH_PROVIDER,
        account_id=account_id,
        display_name=display_name,
        scope=MICROSOFT_OAUTH_SCOPE,
        token_cache=token_cache,
    )
    _PENDING_MICROSOFT_DEVICE_CODES.pop(poll_id, None)
    status_payload = await get_oauth_connection_status(
        db,
        user_id=int(user["id"]),
        provider=MICROSOFT_OAUTH_PROVIDER,
    )
    return {"status": "connected", "connection": status_payload}


@router.delete("/microsoft", status_code=204)
async def microsoft_disconnect(
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> None:
    await deactivate_oauth_connection(
        db,
        user_id=int(user["id"]),
        provider=MICROSOFT_OAUTH_PROVIDER,
    )


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: UserProfileUpdate,
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    if body.username is not None and body.username != user["username"]:
        raise HTTPException(status_code=400, detail="You cannot change your own username")

    if body.new_password:
        if not body.current_password:
            raise HTTPException(status_code=400, detail="current_password required to set a new password")
        if not verify_password(body.current_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")

    username = body.username if body.username is not None else None
    email = body.email if body.email is not None else None
    password_hash = hash_password(body.new_password) if body.new_password else None

    if any(v is not None for v in (username, email, password_hash)):
        try:
            await db.execute(
                """
                UPDATE users
                SET username = COALESCE(?, username),
                    email = COALESCE(?, email),
                    password_hash = COALESCE(?, password_hash)
                WHERE id = ?
                """,
                (username, email, password_hash, user["id"]),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=409, detail="Username already taken")

    async with db.execute(
        "SELECT id, username, email, role, created_at, is_active FROM users WHERE id = ?",
        (user["id"],),
    ) as cur:
        row = await cur.fetchone()
    return UserOut(
        id=row["id"],
        username=row["username"],
        email=row["email"] or "",
        role=row["role"],
        created_at=row["created_at"],
        is_active=bool(row["is_active"]),
    )


# ── Users (admin only) ─────────────────────────────────────────────────────

@router.get("/users", response_model=list[UserOut])
async def list_users(
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute(
        "SELECT id, username, role, created_at, is_active FROM users ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    return [UserOut(**dict(r)) for r in rows]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    hashed = hash_password(body.password)
    try:
        async with db.execute(
            "INSERT INTO users(username, password_hash, role) VALUES(?,?,?) RETURNING id, username, role, created_at, is_active",
            (body.username, hashed, body.role),
        ) as cur:
            row = await cur.fetchone()
        await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail="Username already exists")
    return UserOut(**dict(row))


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    current: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    if user_id == current["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    await db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    await db.commit()


@router.post("/users/{user_id}/activate", status_code=204)
async def activate_user(
    user_id: int,
    current: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    if user_id == current["id"]:
        raise HTTPException(status_code=400, detail="Your account is already active")
    await db.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
    await db.commit()


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: AdminUserUpdate,
    current: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute(
        "SELECT id, username, email, role, created_at, is_active FROM users WHERE id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    if row["id"] == current["id"] and body.username != row["username"]:
        raise HTTPException(status_code=400, detail="You cannot change your own username")

    if body.role is not None and row["id"] == current["id"]:
        raise HTTPException(status_code=400, detail="You cannot change your own role")

    if body.username != row["username"]:
        try:
            await db.execute("UPDATE users SET username = ? WHERE id = ?", (body.username, user_id))
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=409, detail="Username already exists")

    if body.role is not None and body.role != row["role"]:
        await db.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
        await db.commit()

    async with db.execute(
        "SELECT id, username, email, role, created_at, is_active FROM users WHERE id = ?",
        (user_id,),
    ) as cur:
        updated = await cur.fetchone()
    return UserOut(**dict(updated))


# ── Projects ───────────────────────────────────────────────────────────────

@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    _: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute(
        "SELECT id, project_name, project_id, upstream_url, description, repo_path, created_at, is_active FROM projects ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    return [ProjectOut(**dict(r)) for r in rows]


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    pid = _random_project_id()
    try:
        async with db.execute(
            """INSERT INTO projects(project_name, project_id, upstream_url, description, repo_path)
               VALUES(?,?,?,?,?)
               RETURNING id, project_name, project_id, upstream_url, description, repo_path, created_at, is_active""",
            (body.project_name, pid, body.upstream_url, body.description, body.repo_path),
        ) as cur:
            row = await cur.fetchone()
        await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail="project_name already exists")
    return ProjectOut(**dict(row))


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: int,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    await db.execute("UPDATE project_tokens SET is_active = 0 WHERE project_id = ?", (project_id,))
    await db.execute("UPDATE projects SET is_active = 0 WHERE id = ?", (project_id,))
    await db.commit()


@router.post("/projects/{project_id}/restore", status_code=204)
async def restore_project(
    project_id: int,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    await db.execute("UPDATE projects SET is_active = 1 WHERE id = ?", (project_id,))
    await db.commit()


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int,
    body: ProjectUpdate,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    if body.project_name is None and body.upstream_url is None and body.description is None and body.repo_path is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        await db.execute(
            """
            UPDATE projects
            SET project_name = COALESCE(?, project_name),
                upstream_url = COALESCE(?, upstream_url),
                description = COALESCE(?, description),
                repo_path = COALESCE(?, repo_path)
            WHERE id = ?
            """,
            (body.project_name, body.upstream_url, body.description, body.repo_path, project_id),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail="project_name already exists")
    async with db.execute(
        "SELECT id, project_name, project_id, upstream_url, description, repo_path, created_at, is_active FROM projects WHERE id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectOut(**dict(row))


# ── Tokens ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/tokens", response_model=list[ProjectTokenOut])
async def list_tokens(
    project_id: int,
    _: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute(
        "SELECT id, project_id, token_type, token_hint, version, created_at, is_active FROM project_tokens WHERE project_id = ? ORDER BY id",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [ProjectTokenOut(**dict(r)) for r in rows]


@router.post("/projects/{project_id}/tokens", response_model=ProjectTokenOut, status_code=201)
async def generate_project_token(
    project_id: int,
    body: GenerateTokenRequest,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    # Verify project exists
    async with db.execute("SELECT id FROM projects WHERE id = ? AND is_active = 1", (project_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Project not found")

    raw = generate_project_token_value(body.token_type)
    digest = hash_token(raw)
    hint = token_hint(raw)

    async with db.execute(
        """INSERT INTO project_tokens(project_id, token_type, token_hash, token_hint, version)
           VALUES(?,?,?,?,1)
           RETURNING id, project_id, token_type, token_hint, version, created_at, is_active""",
        (project_id, body.token_type, digest, hint),
    ) as cur:
        row = await cur.fetchone()
    await db.commit()

    out = ProjectTokenOut(**dict(row))
    out.token = raw  # one-time reveal
    return out


@router.post("/tokens/{token_id}/rotate", response_model=ProjectTokenOut)
async def rotate_token(
    token_id: int,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Revoke the old token and issue a new one with version+1."""
    async with db.execute(
        "SELECT id, project_id, token_type, version FROM project_tokens WHERE id = ? AND is_active = 1",
        (token_id,),
    ) as cur:
        old = await cur.fetchone()
    if not old:
        raise HTTPException(status_code=404, detail="Token not found or already revoked")

    new_version = old["version"] + 1
    raw = generate_project_token_value(old["token_type"])
    digest = hash_token(raw)
    hint = token_hint(raw)

    # Revoke old, insert new in one transaction
    await db.execute("UPDATE project_tokens SET is_active = 0 WHERE id = ?", (token_id,))
    async with db.execute(
        """INSERT INTO project_tokens(project_id, token_type, token_hash, token_hint, version)
           VALUES(?,?,?,?,?)
           RETURNING id, project_id, token_type, token_hint, version, created_at, is_active""",
        (old["project_id"], old["token_type"], digest, hint, new_version),
    ) as cur:
        row = await cur.fetchone()
    await db.commit()

    out = ProjectTokenOut(**dict(row))
    out.token = raw  # one-time reveal
    return out


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: int,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    await db.execute("UPDATE project_tokens SET is_active = 0 WHERE id = ?", (token_id,))
    await db.commit()


# ── Job Status / Indexing Progress ─────────────────────────────────────────

@router.get("/projects/index-status", response_model=list[ProjectIndexStatus])
async def list_projects_index_status(
    _: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
    consumer = Depends(get_consumer),
    registry = Depends(get_registry),
):
    """Get indexing status for all projects."""

    # Get all projects
    async with db.execute(
        "SELECT id, project_name, project_id, repo_path FROM projects WHERE is_active = 1 ORDER BY id"
    ) as cur:
        projects = await cur.fetchall()

    queue_snapshot = None
    if consumer:
        try:
            queue_snapshot = await consumer.get_queue_snapshot()
        except Exception:
            queue_snapshot = None

    pending_jobs = queue_snapshot.get("pending_jobs", []) if queue_snapshot else []
    processing_jobs = queue_snapshot.get("processing_jobs", []) if queue_snapshot else []
    avg_duration_sec = int(queue_snapshot.get("avg_duration_sec", 30)) if queue_snapshot else 30

    pending_by_id = {
        str(job.get("job_id", "")): idx + 1
        for idx, job in enumerate(pending_jobs)
        if job.get("job_id")
    }

    current_processing = processing_jobs[0] if processing_jobs else None
    processing_remaining = (
        _estimate_processing_remaining(current_processing, avg_duration_sec)
        if current_processing
        else 0
    )

    results = []
    for proj in projects:
        proj_dict = dict(proj)
        latest_job = None
        recent_jobs: list[IndexJobStatus] = []

        # Get latest job status for this project (if consumer available)
        if consumer:
            try:
                repo_patterns = _project_repo_status_paths(proj_dict)

                jobs: list[dict] = []
                seen_job_ids: set[str] = set()
                for pattern in repo_patterns:
                    matching = await consumer.get_jobs_by_repo(pattern)
                    if not matching:
                        continue
                    for item in matching:
                        job_id = str(item.get("job_id", ""))
                        if job_id and job_id in seen_job_ids:
                            continue
                        jobs.append(item)
                        if job_id:
                            seen_job_ids.add(job_id)

                jobs.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

                if jobs:
                    latest_job = _build_index_job_status(
                        jobs[0],
                        pending_by_id,
                        avg_duration_sec,
                        processing_remaining,
                    )
                    recent_jobs = [
                        _build_index_job_status(job_data, pending_by_id, avg_duration_sec, processing_remaining)
                        for job_data in jobs[:5]
                    ]
            except Exception:
                pass  # Silently ignore consumer errors

        results.append(ProjectIndexStatus(
            project_id=proj_dict["id"],
            project_name=proj_dict["project_name"],
            latest_job=latest_job,
            recent_jobs=recent_jobs,
            graph_stats=_query_graph_live_stats(registry, proj_dict["project_name"]),
        ))

    return results


@router.post("/projects/{project_id}/index", response_model=ProjectIndexTriggerOut)
async def trigger_project_index(
    project_id: int,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    project = await _get_active_project(project_id, db)

    repo_path = _resolve_project_repo_path(project)
    if not repo_path:
        raise HTTPException(
            status_code=404,
            detail=f"Repository path not found for project_name '{project['project_name']}'",
        )

    result = await mcp_server.index_repo_changes(repo_path=repo_path, project_name=project["project_name"])
    return ProjectIndexTriggerOut(
        project_id=project["id"],
        project_name=project["project_name"],
        repo_path=repo_path,
        status=result.get("status", "queued"),
        mode=result.get("mode", "incremental"),
        job_id=result.get("job_id"),
        stream_id=result.get("stream_id"),
        changed_count=int(result.get("changed_count") or 0),
        destructive_count=int(result.get("destructive_count") or 0),
        reason=result.get("reason"),
    )


@router.post("/projects/{project_id}/index-full", response_model=ProjectIndexTriggerOut)
async def trigger_project_full_index(
    project_id: int,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    project = await _get_active_project(project_id, db)

    repo_path = _resolve_project_repo_path(project)
    if not repo_path:
        raise HTTPException(
            status_code=404,
            detail=f"Repository path not found for project_name '{project['project_name']}'",
        )

    result = await mcp_server.index_full(repo_path=repo_path, project_name=project["project_name"])
    return ProjectIndexTriggerOut(
        project_id=project["id"],
        project_name=project["project_name"],
        repo_path=repo_path,
        status=result.get("status", "queued"),
        mode="full",
        job_id=result.get("job_id"),
        stream_id=result.get("stream_id"),
        changed_count=0,
        destructive_count=0,
        reason="admin_triggered_full",
    )


@router.post("/projects/{project_id}/index/recover-stale", response_model=ProjectIndexRecoveryOut)
async def recover_project_stale_index_jobs(
    project_id: int,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
    consumer = Depends(get_consumer),
):
    if not consumer:
        raise HTTPException(status_code=503, detail="Indexer consumer is unavailable")

    project = await _get_active_project(project_id, db)
    repo_path = _resolve_project_repo_path(project)
    if not repo_path:
        raise HTTPException(
            status_code=404,
            detail=f"Repository path not found for project_name '{project['project_name']}'",
        )

    repo_paths = _project_repo_status_paths(project)
    recovered = await consumer.recover_stale_jobs_by_repo(repo_paths, INDEX_STALE_AFTER_SEC)
    recovered_jobs = [_build_index_job_status(job, {}, 30, 0) for job in recovered]
    return ProjectIndexRecoveryOut(
        project_id=project["id"],
        project_name=project["project_name"],
        repo_path=repo_path,
        recovered_count=len(recovered_jobs),
        recovered_jobs=recovered_jobs,
    )


@router.get("/audit", response_model=list[AuditLogOut])
async def list_audit_logs(
    limit: int = 100,
    offset: int = 0,
    scope: str | None = None,
    actor: str | None = None,
    project_name: str | None = None,
    path_like: str | None = None,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Admin-only audit query for API/MCP activity and token operations."""
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, offset)

    clauses: list[str] = []
    params: list[object] = []

    if scope:
        clauses.append("scope = ?")
        params.append(scope.strip().lower())
    if actor:
        clauses.append("LOWER(COALESCE(actor_name, '')) LIKE ?")
        params.append(f"%{actor.strip().lower()}%")
    if project_name:
        clauses.append("LOWER(COALESCE(project_name, '')) LIKE ?")
        params.append(f"%{project_name.strip().lower()}%")
    if path_like:
        clauses.append("LOWER(path) LIKE ?")
        params.append(f"%{path_like.strip().lower()}%")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT id, created_at, scope, method, path, status_code, duration_ms,
               actor_type, actor_id, actor_name,
                             project_id, project_name, token_id,
             client_ip, user_agent, query_string, request_body, response_error, details_json, token_usage_total
        FROM audit_logs
        {where_sql}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([safe_limit, safe_offset])

    async with db.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
    return [AuditLogOut(**dict(r)) for r in rows]


@router.get("/audit/paged", response_model=PaginatedAuditOut)
async def list_audit_logs_paged(
    page: int = 1,
    page_size: int = 25,
    scope: str | None = None,
    actor: str | None = None,
    project_name: str | None = None,
    path_like: str | None = None,
    _: dict = Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    safe_page = max(1, page)
    safe_page_size = max(1, min(page_size, 200))
    safe_offset = (safe_page - 1) * safe_page_size

    clauses: list[str] = []
    params: list[object] = []

    if scope:
        clauses.append("scope = ?")
        params.append(scope.strip().lower())
    if actor:
        clauses.append("LOWER(COALESCE(actor_name, '')) LIKE ?")
        params.append(f"%{actor.strip().lower()}%")
    if project_name:
        clauses.append("LOWER(COALESCE(project_name, '')) LIKE ?")
        params.append(f"%{project_name.strip().lower()}%")
    if path_like:
        clauses.append("LOWER(path) LIKE ?")
        params.append(f"%{path_like.strip().lower()}%")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    count_sql = f"SELECT COUNT(*) AS total FROM audit_logs {where_sql}"
    async with db.execute(count_sql, tuple(params)) as cur:
        count_row = await cur.fetchone()
    total = int(count_row["total"]) if count_row else 0

    data_sql = f"""
        SELECT id, created_at, scope, method, path, status_code, duration_ms,
               actor_type, actor_id, actor_name,
               project_id, project_name, token_id,
               client_ip, user_agent, query_string, request_body, response_error, details_json, token_usage_total
        FROM audit_logs
        {where_sql}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    data_params = [*params, safe_page_size, safe_offset]
    async with db.execute(data_sql, tuple(data_params)) as cur:
        rows = await cur.fetchall()

    return PaginatedAuditOut(
        items=[AuditLogOut(**dict(r)) for r in rows],
        total=total,
        page=safe_page,
        page_size=safe_page_size,
    )
