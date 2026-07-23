"""FastAPI dependencies: current user, admin guard, token validation."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.auth.database import get_db
from backend.auth.pgshim import Connection
from backend.auth.security import JWTError, decode_access_token

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Connection = Depends(get_db),
) -> dict:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not creds:
        raise exc
    try:
        payload = decode_access_token(creds.credentials)
        username: str = payload.get("sub", "")
    except JWTError:
        raise exc

    async with db.execute(
        "SELECT id, username, email, role, password_hash, is_active FROM users WHERE username = ?",
        (username,),
    ) as cur:
        row = await cur.fetchone()

    if not row or not row["is_active"]:
        raise exc
    return dict(row)


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


async def get_consumer(request: Request):
    """Get IndexerConsumer from app state."""
    return getattr(request.app.state, 'consumer', None)


async def get_registry(request: Request):
    """Get GraphRegistry from app state."""
    return getattr(request.app.state, 'registry', None)
