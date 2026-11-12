from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.maintenance.spool import pending


class MaintenanceGate:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        protected = path.startswith(("/api", "/mcp"))
        mutating = protected and method not in {"GET", "HEAD", "OPTIONS"}
        if (mutating or path.startswith("/mcp")) and pending() and path != "/api/admin/updates/cancel":
            response = JSONResponse(
                {"detail": "Upgrade maintenance is draining writes. Retry after completion."},
                status_code=503, headers={"Retry-After": "30"},
            )
            await response(scope, receive, send)
            return
        state = scope["app"].state
        if mutating:
            state.maintenance_inflight = getattr(state, "maintenance_inflight", 0) + 1
        try:
            # Retain the counter until the complete ASGI response, not only its headers.
            await self.app(scope, receive, send)
        finally:
            if mutating:
                state.maintenance_inflight -= 1
