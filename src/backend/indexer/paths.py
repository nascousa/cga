"""Repository path boundaries shared by request handlers and index workers."""

from __future__ import annotations

import ntpath
import os
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath


class RepositoryPathError(ValueError):
    """A path is not a member of the selected repository."""


def _clean_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RepositoryPathError("A non-empty repository path is required")
    normalized = value.replace("\\", "/")
    if normalized.startswith("//"):
        raise RepositoryPathError("Network and device paths are not allowed")
    return normalized


def normalize_repo_path(repo_path: str) -> str:
    """Normalize a directory or filename, including missing mapped tombstones.

    This performs no project authorization. Use ``resolve_repo_root`` for an
    existing checkout and ``resolve_changed_path`` for file membership checks.
    """
    normalized = _clean_path(repo_path)
    windows = PureWindowsPath(normalized)
    if windows.drive and not windows.is_absolute():
        raise RepositoryPathError("Drive-relative paths are not allowed")
    candidate = Path(normalized)
    if windows.is_absolute() and not candidate.is_absolute():
        parts = windows.parts
        marker = next((i for i, part in enumerate(parts) if part.casefold() == "repos"), None)
        if marker is not None:
            tail = parts[marker + 1:]
            if not tail or any(part in {".", ".."} or ":" in part for part in tail):
                raise RepositoryPathError("Invalid container repository mapping")
            candidate = Path("/repos").joinpath(*tail)
            return str(candidate)
        return normalized
    return str(candidate)


def _lexical_identity(value: str) -> tuple[str, str]:
    cleaned = _clean_path(value)
    windows = PureWindowsPath(cleaned)
    if windows.drive:
        if not windows.is_absolute():
            raise RepositoryPathError("Drive-relative paths are not allowed")
        return "windows", ntpath.normcase(ntpath.normpath(cleaned))
    if os.name == "nt" and not cleaned.startswith("/"):
        return "relative-windows", ntpath.normcase(ntpath.normpath(cleaned))
    return "posix", posixpath.normpath(cleaned)


def matches_registered_root(requested: str, registered: str, canonical: Path) -> bool:
    """Compare logical aliases without inspecting any request-controlled path."""
    aliases = {_lexical_identity(registered), _lexical_identity(str(canonical))}
    registration = _clean_path(registered)
    windows = PureWindowsPath(registration)
    mapped = registration
    if windows.is_absolute():
        marker = next((i for i, part in enumerate(windows.parts) if part.casefold() == "repos"), None)
        if marker is not None:
            tail = windows.parts[marker + 1:]
            if tail and all(part not in {".", ".."} and ":" not in part for part in tail):
                mapped = str(PurePosixPath("/repos").joinpath(*tail))
                aliases.add(_lexical_identity(mapped))
    # The historical desktop mount defaults to D:/Repos. Other host mounts must
    # be configured by the operator, never inferred from a request's basename.
    for trusted_path in (mapped, _clean_path(str(canonical))):
        try:
            relative = PurePosixPath(posixpath.normpath(trusted_path)).relative_to("/repos")
        except ValueError:
            continue
        host_root = _clean_path(os.getenv("CGA_HOST_REPOS_ROOT", "D:/Repos"))
        host = PureWindowsPath(host_root)
        if host.is_absolute():
            alias = str(host.joinpath(*relative.parts))
        elif PurePosixPath(host_root).is_absolute():
            alias = str(PurePosixPath(host_root).joinpath(*relative.parts))
        else:
            raise RepositoryPathError("CGA_HOST_REPOS_ROOT must be absolute")
        # An explicit registered Windows drive remains the authority.
        if not windows.is_absolute():
            aliases.add(_lexical_identity(alias))
    return _lexical_identity(requested) in aliases


def resolve_repo_root(repo_path: str) -> Path:
    """Resolve an existing checkout, including Windows-to-container mounts.

    This establishes a filesystem boundary, not project authorization. HTTP/MCP
    callers must first bind the checkout to a registered project.
    """
    candidate = Path(normalize_repo_path(repo_path))
    if PureWindowsPath(str(candidate)).is_absolute() and not candidate.is_absolute():
        raise FileNotFoundError("Windows repository path has no visible container mapping")
    if not candidate.is_dir():
        raise FileNotFoundError("Repository is not visible to the CGA indexer")
    return candidate.resolve(strict=True)


def resolve_changed_path(repo_path: str, resolved_root: Path, changed_path: str) -> str:
    """Return a canonical absolute path inside ``resolved_root``.

    Missing files are supported for deletion/tombstone jobs. Existing symlinks,
    including symlinked parents of missing files, are resolved before checking
    containment. Absolute Windows paths may only be translated relative to the
    original repository argument, never by their basename or current directory.
    """
    normalized = _clean_path(changed_path)
    root = resolved_root.resolve(strict=True)
    if not root.is_dir():
        raise FileNotFoundError("Repository is not visible to the CGA indexer")
    windows = PureWindowsPath(normalized)
    candidate = Path(normalized)
    if windows.drive and not windows.is_absolute():
        raise RepositoryPathError("Drive-relative paths are not allowed")
    if windows.is_absolute() and not candidate.is_absolute():
        original = PureWindowsPath(_clean_path(repo_path))
        if not original.is_absolute():
            raise RepositoryPathError("Absolute path is outside the registered repository")
        try:
            relative = windows.relative_to(original)
        except ValueError as exc:
            raise RepositoryPathError("Absolute path is outside the registered repository") from exc
        candidate = root.joinpath(*relative.parts)
    elif not candidate.is_absolute():
        if normalized.startswith("/") or windows.root:
            raise RepositoryPathError("Root-relative paths are not allowed")
        candidate = root / candidate

    # Reject NTFS alternate data streams and drive-like components on every host.
    parts = candidate.parts[1:] if candidate.is_absolute() else candidate.parts
    if any(":" in part for part in parts):
        raise RepositoryPathError("Alternate data streams are not allowed")
    lexical = os.path.abspath(str(candidate))
    root_prefix = os.path.join(str(root), "")
    if not os.path.normcase(lexical).startswith(os.path.normcase(root_prefix)):
        raise RepositoryPathError("File path is outside the registered repository")
    relative = Path(lexical).relative_to(root)
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise RepositoryPathError("File path is outside the registered repository")
    # Rebuild under the trusted root from individual, validated filenames.
    bounded = root.joinpath(*(os.path.basename(part) for part in relative.parts))
    resolved = bounded.resolve()
    if not os.path.normcase(str(resolved)).startswith(os.path.normcase(root_prefix)):
        raise RepositoryPathError("File path is outside the registered repository")
    return str(resolved)
