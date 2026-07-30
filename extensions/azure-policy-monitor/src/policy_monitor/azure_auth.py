"""Azure access-token providers for local and hosted monitor execution."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


class AzureTokenError(RuntimeError):
    pass


class _CredentialUnavailable(AzureTokenError):
    pass


class AzureTokenTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        form: dict[str, str] | None,
        timeout_seconds: float,
    ) -> "AzureTokenHttpResponse": ...


@dataclass(frozen=True)
class AzureTokenHttpResponse:
    status_code: int
    payload: dict[str, Any]


class UrllibAzureTokenTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        form: dict[str, str] | None,
        timeout_seconds: float,
    ) -> AzureTokenHttpResponse:
        body = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
        request_headers = dict(headers)
        if form is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return AzureTokenHttpResponse(int(response.status), _decode_json(response.read()))
        except urllib.error.HTTPError as exc:
            return AzureTokenHttpResponse(int(exc.code), _decode_json(exc.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AzureTokenError(f"Azure credential endpoint is unavailable: {type(exc).__name__}") from exc


CommandRunner = Callable[[list[str], float], tuple[int, str, str]]


class DefaultAzureAccessTokenProvider:
    """Resolve an Azure token without persisting credentials in extension config."""

    VALID_AUTH_MODES = {"auto", "environment", "workload_identity", "managed_identity", "azure_cli"}

    def __init__(
        self,
        *,
        auth_mode: str = "auto",
        authority_host: str = "https://login.microsoftonline.com",
        managed_identity_client_id: str = "",
        environment: Mapping[str, str] | None = None,
        transport: AzureTokenTransport | None = None,
        command_runner: CommandRunner | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        mode = str(auth_mode or "auto").strip().lower()
        if mode not in self.VALID_AUTH_MODES:
            raise ValueError(f"Azure auth_mode must be one of: {', '.join(sorted(self.VALID_AUTH_MODES))}.")
        authority = str(authority_host or "").rstrip("/")
        parsed = urllib.parse.urlparse(authority)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Azure authority_host must be an absolute HTTPS URL.")
        self.auth_mode = mode
        self.authority_host = authority
        self.managed_identity_client_id = str(managed_identity_client_id or "").strip()
        self.environment = environment if environment is not None else os.environ
        self.transport = transport or UrllibAzureTokenTransport()
        self.command_runner = command_runner or _run_command
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self.clock = clock
        self._cache: dict[str, tuple[str, float]] = {}

    def get_token(self, resource: str) -> str:
        scope = str(resource or "").strip()
        if not scope:
            raise AzureTokenError("Azure token scope is required.")
        cached = self._cache.get(scope)
        now = self.clock()
        if cached and cached[1] - now > 120:
            return cached[0]

        if self.auth_mode in {"environment", "workload_identity"}:
            return self._environment_token(scope)
        if self.auth_mode == "managed_identity":
            return self._managed_identity_token(scope, allow_imds=True)
        if self.auth_mode == "azure_cli":
            return self._azure_cli_token(scope)

        unavailable: list[str] = []
        for name, loader in (
            ("environment", lambda: self._environment_token(scope)),
            ("managed identity endpoint", lambda: self._managed_identity_token(scope, allow_imds=False)),
            ("Azure CLI", lambda: self._azure_cli_token(scope)),
            ("IMDS managed identity", lambda: self._managed_identity_token(scope, allow_imds=True, imds_only=True)),
        ):
            try:
                return loader()
            except _CredentialUnavailable:
                unavailable.append(name)
        raise AzureTokenError(f"No Azure credential was available ({', '.join(unavailable)}).")

    def _environment_token(self, scope: str) -> str:
        tenant_id = self._env("AZURE_TENANT_ID")
        client_id = self._env("AZURE_CLIENT_ID")
        client_secret = self._env("AZURE_CLIENT_SECRET")
        assertion_path = self._env("AZURE_FEDERATED_TOKEN_FILE")
        if not tenant_id and not client_id and not client_secret and not assertion_path:
            raise _CredentialUnavailable("Environment credential is not configured.")
        if not tenant_id or not client_id:
            raise AzureTokenError("Environment credential requires AZURE_TENANT_ID and AZURE_CLIENT_ID.")
        if client_secret and assertion_path:
            raise AzureTokenError("Configure either AZURE_CLIENT_SECRET or AZURE_FEDERATED_TOKEN_FILE, not both.")
        form = {
            "client_id": client_id,
            "grant_type": "client_credentials",
            "scope": scope,
        }
        if client_secret:
            form["client_secret"] = client_secret
        elif assertion_path:
            try:
                assertion = Path(assertion_path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise AzureTokenError("Azure federated token file could not be read.") from exc
            if not assertion:
                raise AzureTokenError("Azure federated token file is empty.")
            form.update(
                {
                    "client_assertion": assertion,
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                }
            )
        else:
            raise AzureTokenError("Environment credential requires a client secret or federated token file.")
        tenant_segment = urllib.parse.quote(tenant_id, safe="")
        response = self.transport.request(
            "POST",
            f"{self.authority_host}/{tenant_segment}/oauth2/v2.0/token",
            headers={"Accept": "application/json"},
            form=form,
            timeout_seconds=self.timeout_seconds,
        )
        return self._accept_token(scope, response, "environment credential")

    def _managed_identity_token(self, scope: str, *, allow_imds: bool, imds_only: bool = False) -> str:
        resource = _resource_from_scope(scope)
        endpoint = "" if imds_only else self._env("IDENTITY_ENDPOINT")
        identity_header = self._env("IDENTITY_HEADER")
        headers = {"Accept": "application/json"}
        api_version = "2019-08-01"
        if endpoint:
            if not identity_header:
                raise AzureTokenError("IDENTITY_ENDPOINT is set but IDENTITY_HEADER is missing.")
            _validate_local_identity_endpoint(endpoint)
            headers["X-IDENTITY-HEADER"] = identity_header
        elif not imds_only and self._env("MSI_ENDPOINT"):
            endpoint = self._env("MSI_ENDPOINT")
            _validate_local_identity_endpoint(endpoint)
            msi_secret = self._env("MSI_SECRET")
            if not msi_secret:
                raise AzureTokenError("MSI_ENDPOINT is set but MSI_SECRET is missing.")
            headers["secret"] = msi_secret
            api_version = "2017-09-01"
        elif allow_imds:
            endpoint = "http://169.254.169.254/metadata/identity/oauth2/token"
            headers["Metadata"] = "true"
            api_version = "2018-02-01"
        else:
            raise _CredentialUnavailable("Managed identity endpoint is not configured.")

        query = {"api-version": api_version, "resource": resource}
        client_id = self.managed_identity_client_id or self._env("AZURE_CLIENT_ID")
        if client_id:
            query["client_id"] = client_id
        separator = "&" if "?" in endpoint else "?"
        response = self.transport.request(
            "GET",
            f"{endpoint}{separator}{urllib.parse.urlencode(query)}",
            headers=headers,
            form=None,
            timeout_seconds=min(self.timeout_seconds, 5.0),
        )
        return self._accept_token(scope, response, "managed identity")

    def _azure_cli_token(self, scope: str) -> str:
        resource = _resource_from_scope(scope)
        try:
            return_code, stdout, _stderr = self.command_runner(
                ["az", "account", "get-access-token", "--resource", resource, "--output", "json"],
                self.timeout_seconds,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise _CredentialUnavailable("Azure CLI is unavailable.") from exc
        if return_code != 0:
            raise _CredentialUnavailable("Azure CLI has no active authenticated account.")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AzureTokenError("Azure CLI returned invalid token JSON.") from exc
        if not isinstance(payload, dict):
            raise AzureTokenError("Azure CLI returned invalid token data.")
        return self._accept_token(scope, AzureTokenHttpResponse(200, payload), "Azure CLI")

    def _accept_token(self, scope: str, response: AzureTokenHttpResponse, source: str) -> str:
        if response.status_code >= 400:
            error_code = str(response.payload.get("error") or "authentication_failed")[:100]
            raise AzureTokenError(f"Azure {source} failed ({response.status_code}, {error_code}).")
        token = str(response.payload.get("access_token") or response.payload.get("accessToken") or "").strip()
        if not token:
            raise AzureTokenError(f"Azure {source} returned an empty access token.")
        expires_in = _positive_float(response.payload.get("expires_in") or response.payload.get("expiresIn"), 300.0)
        self._cache[scope] = (token, self.clock() + expires_in)
        return token

    def _env(self, name: str) -> str:
        return str(self.environment.get(name, "") or "").strip()


def _run_command(command: list[str], timeout_seconds: float) -> tuple[int, str, str]:
    executable = shutil.which(command[0])
    if not executable:
        raise FileNotFoundError(command[0])
    completed = subprocess.run(
        [executable, *command[1:]],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _resource_from_scope(scope: str) -> str:
    suffix = "/.default"
    return scope[: -len(suffix)] if scope.endswith(suffix) else scope


def _validate_local_identity_endpoint(endpoint: str) -> None:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AzureTokenError("Managed identity endpoint must be an absolute HTTP(S) URL.")
    if parsed.scheme == "http" and parsed.hostname.lower() not in {
        "127.0.0.1",
        "localhost",
        "::1",
        "169.254.169.254",
    }:
        raise AzureTokenError("Plain-HTTP managed identity endpoint must be local or link-local.")


def _decode_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _positive_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback
