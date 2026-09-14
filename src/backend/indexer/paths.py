"""Repository path boundaries shared by request handlers and index workers."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath


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
            mount = Path("/repos").resolve()
            candidate = (mount / Path(*tail)).resolve()
            if not candidate.is_relative_to(mount):
                raise RepositoryPathError("Repository mapping escapes the repositories mount")
            return str(candidate)
        return normalized
    if candidate.exists():
        return str(candidate.resolve(strict=True))
    return str(candidate)


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
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise RepositoryPathError("File path is outside the registered repository")
    return str(resolved)
