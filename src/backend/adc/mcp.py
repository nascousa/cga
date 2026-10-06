from __future__ import annotations

from backend.adc.remote import RemoteQuery, query
from backend.auth.context import _current_project_db_id
from backend.auth.pgshim import get_pool


async def adc_call(operation: str, revision: int | None = None, release_id: int | None = None,
                   path: str | None = None, offset: int = 0, limit: int = 50) -> dict:
    project = _current_project_db_id.get()
    if project <= 0:
        raise ValueError("Authenticated project context is required for ADC")
    request = RemoteQuery(operation=operation, revision=revision, release_id=release_id,
                          path=path, offset=offset, limit=limit)
    async with get_pool().acquire() as db:
        return await query(db.raw, project, request)


def register(mcp):
    @mcp.tool()
    async def adc_catalog(offset: int = 0, limit: int = 50) -> dict:
        """List immutable ADC releases and the latest CGA-published version; does not upgrade projects."""
        return await adc_call("adc_catalog", offset=offset, limit=limit)

    @mcp.tool()
    async def adc_release(release_id: int) -> dict:
        """Read an immutable global ADC release for review, not automatic project adoption."""
        return await adc_call("adc_release", release_id=release_id)

    @mcp.tool()
    async def adc_current(revision: int | None = None) -> dict:
        """Read this authenticated project's pinned ADC, effective rules and exceptions."""
        return await adc_call("adc_current", revision=revision)

    @mcp.tool()
    async def adc_history(offset: int = 0, limit: int = 50) -> dict:
        """Read this project's immutable ADC revision history."""
        return await adc_call("adc_history", offset=offset, limit=limit)

    @mcp.tool()
    async def adc_diff(release_id: int) -> dict:
        """Compare this project's baseline to a proposed ADC release and flag affected overrides."""
        return await adc_call("adc_diff", release_id=release_id)

    @mcp.tool()
    async def adc_document(path: str, revision: int | None = None) -> dict:
        """Read one effective ADC document and its SHA256 from this project's approved revision."""
        return await adc_call("adc_document", path=path, revision=revision)

    @mcp.tool()
    async def adc_bundle(revision: int | None = None) -> dict:
        """Return this project's effective ADC files, SHA256 hashes and provenance lock; never writes server files."""
        return await adc_call("adc_bundle", revision=revision)
