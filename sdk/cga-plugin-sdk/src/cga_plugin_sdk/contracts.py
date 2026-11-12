"""Manifest v1 and the existing scheduler's four-field adapter result."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Version = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")]
Permission = Literal[
    "project.read", "activity.write", "network.outbound", "secrets.read",
    "filesystem.read", "filesystem.write", "scheduler.execute",
]
HostResult = tuple[Literal["success", "failed"], int | None, dict, str]
CORE_IDS = {"adc", "audit", "identity", "projects", "graph", "mcp", "memory", "scheduler", "backup"}


def resource_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or not path.parts or "\\" in value or ":" in value or path.is_absolute()
            or str(path) != value or any(part in {"..", ".", ".git"} for part in path.parts)
            or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part) or part.endswith(".")
                   or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL",
                                                       *(f"COM{i}" for i in range(1, 10)),
                                                       *(f"LPT{i}" for i in range(1, 10))}
                   for part in path.parts)
            or path.name.startswith(".env") or path.suffix.lower() in {".key", ".pem", ".pfx", ".p12"}):
        raise ValueError("Resources must be canonical relative paths without secret/key files.")
    return value


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_extra={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:cga:plugin:manifest:1",
    })
    schema_version: Literal[1]
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
    name: str = Field(min_length=1, max_length=100)
    version: Version
    sdk_api: Literal["cga.first-party.v1"]
    execution_model: Literal["first-party-in-process"]
    min_cga_version: Version
    entrypoint: str = Field(pattern=r"^[a-z_][a-z0-9_]*:execute$")
    license: Literal["Apache-2.0", "MIT", "BSD-3-Clause"]
    permissions: list[Permission] = Field(max_length=7)
    network_hosts: list[str] = Field(max_length=32)
    secret_env: list[str] = Field(max_length=32)
    timeout_seconds: int = Field(ge=1, le=180)
    retention_days: int = Field(ge=1, le=365)
    resources: list[str] = Field(min_length=1, max_length=128)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema(cls, value):
        if type(value) is not int:
            raise ValueError("Schema version must be an integer.")
        return value

    @model_validator(mode="after")
    def validate_contract(self):
        if self.id in CORE_IDS:
            raise ValueError("Core capability IDs cannot be registered as plugins.")
        if not self.name.strip():
            raise ValueError("A plugin name is required.")
        for field in ("permissions", "network_hosts", "secret_env", "resources"):
            values = getattr(self, field)
            if len({value.casefold() for value in values}) != len(values):
                raise ValueError(f"{field} contains duplicate entries.")
        for path in self.resources:
            resource_path(path)
            if path.casefold() in {"plugin.json", "sha256sums.txt"}:
                raise ValueError("Manifest and checksum names are reserved.")
        module = self.entrypoint.split(":")[0] + ".py"
        if module not in self.resources:
            raise ValueError("Entrypoint source must be a declared resource.")
        if "LICENSE" not in self.resources:
            raise ValueError("A distributable LICENSE resource is required.")
        if any(len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")) for host in self.network_hosts):
            raise ValueError("Network hosts must be explicit lowercase DNS names, without wildcards or URLs.")
        if bool(self.network_hosts) != ("network.outbound" in self.permissions):
            raise ValueError("Outbound permission and explicit network hosts must be declared together.")
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name) for name in self.secret_env):
            raise ValueError("Secrets must reference uppercase environment variable names, not values.")
        if bool(self.secret_env) != ("secrets.read" in self.permissions):
            raise ValueError("Secret permission and environment references must be declared together.")
        return self


class ScheduledTask(Protocol):
    @property
    def project_external_id(self) -> str | None: ...

    @property
    def payload(self) -> dict: ...


@dataclass(frozen=True)
class ExecutionResult:
    state: Literal["success", "failed"]
    payload: dict
    error: str = ""
    response_status: int | None = None

    def to_host_result(self) -> HostResult:
        if not isinstance(self.payload, dict) or not isinstance(self.error, str):
            raise ValueError("Result requires an object payload and a text error.")
        if self.state not in {"success", "failed"}:
            raise ValueError("Result state must be success or failed.")
        if (self.state == "failed") != bool(self.error.strip()):
            raise ValueError("Failures require an error; successes must not contain one.")
        if self.response_status is not None and (type(self.response_status) is not int
                                                or not 100 <= self.response_status <= 599):
            raise ValueError("Invalid response status.")
        if self.state == "success" and self.response_status is not None and self.response_status >= 400:
            raise ValueError("An error response cannot be reported as success.")
        encoded = json.dumps(self.payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 65536:
            raise ValueError("Plugin result exceeds 64 KiB; return bounded evidence instead.")
        return self.state, self.response_status, self.payload, self.error
