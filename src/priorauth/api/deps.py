"""Dependencies shared by the API routes: the bearer token and the role check (see priorauth.security.auth)."""

import re
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from priorauth.security.audit import AuditLog
from priorauth.security.auth import AuthError, Principal, Role, Users

UUID_SHAPE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
bearer = HTTPBearer(auto_error=False, description="JWT from POST /auth/token")


def require(*roles: Role) -> Callable[..., Any]:
    """Dependency: the caller's Principal if its token is valid and its role is one of `roles`; else 401 or 403,
    both written to the audit log with the route template (never the path, which holds ids)."""

    async def dependency(
        request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> Principal:
        users: Users = request.app.state.users
        audit: AuditLog = request.app.state.audit
        route = getattr(request.scope.get("route"), "path", "unknown")
        try:
            if credentials is None:
                raise AuthError(401, "no_token")
            principal = await users.principal(request.app.state.tokens.subject(credentials.credentials))
        except AuthError as e:
            await audit.record("anonymous", "none", "access.denied", route, status=401, reason=e.reason)
            raise HTTPException(
                status_code=401, detail="a valid bearer token is required", headers={"WWW-Authenticate": "Bearer"}
            ) from None
        if principal.role not in roles:
            await audit.record(principal.user_id, principal.role, "access.denied", route, status=403, reason="role")
            raise HTTPException(status_code=403, detail=f"role {principal.role!r} may not use this route")
        return principal

    return dependency


SUBMITTER = Depends(require("system", "admin"))
READER = Depends(require("system", "reviewer", "admin"))
ADMIN = Depends(require("admin"))
REVIEWER = Depends(require("reviewer", "admin"))
