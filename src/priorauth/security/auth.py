"""Authentication and role-based access control (the principles of 45 CFR 164.312(a) and (d)).

- Users: one row per person or service, a unique opaque id (UUID), a unique username, exactly one role, a scrypt
  password hash, an `active` flag. Roles: `system` (a submitting system: submits cases, reads their status and
  result), `reviewer` (a human reviewer: reads cases, results and records reviews), `admin` (everything, plus costs
  and the audit log).
- Tokens: JWT signed with HS256 (PRIORAUTH_JWT_SECRET), valid 15 minutes, with sub (user id), role, iat, exp, jti
  and iss. Every request re-reads the user, so a deactivated user or a changed role takes effect at once.
- Errors: no or bad token -> 401 with `WWW-Authenticate: Bearer`; a valid token without the role -> 403. Both are
  written to the audit log.

Not done: no lockout or rate limit on failed logins, no MFA, no token revocation list (tokens are short-lived and the
user is re-checked), no password policy beyond a minimum length.
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

import jwt
from sqlalchemy import Boolean, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from priorauth.jobs.db import Base, utcnow

Role = Literal["system", "reviewer", "admin"]
ROLES: tuple[Role, ...] = get_args(Role)
ISSUER = "priorauth-agent"
ALGORITHM = "HS256"
TOKEN_TTL = timedelta(minutes=15)
MIN_SECRET_BYTES = 32
MIN_PASSWORD_CHARS = 12
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    computed = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **SCRYPT)
    return hmac.compare_digest(computed, base64.b64decode(digest))


# Checked when the username is unknown, so that a login takes the same time whether or not the user exists.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: Role


class AuthError(Exception):
    """`status` is 401 (who are you?) or 403 (not allowed); `reason` is a short code for the audit log."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class TokenIssuer:
    def __init__(
        self, secret: bytes, ttl: timedelta = TOKEN_TTL, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        if len(secret) < MIN_SECRET_BYTES:
            raise ValueError(f"the JWT secret must be at least {MIN_SECRET_BYTES} bytes")
        self.secret = secret
        self.ttl = ttl
        self.clock = clock

    def issue(self, user: UserRow) -> str:
        now = self.clock()
        claims = {
            "sub": user.id,
            "role": user.role,
            "iat": int(now.timestamp()),
            "exp": int((now + self.ttl).timestamp()),
            "jti": str(uuid.uuid4()),
            "iss": ISSUER,
        }
        return jwt.encode(claims, self.secret, algorithm=ALGORITHM)

    def subject(self, token: str) -> str:
        """The user id of a valid token; AuthError(401) otherwise (bad signature, expired, wrong issuer, ...)."""
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=[ALGORITHM],
                issuer=ISSUER,
                options={"require": ["sub", "role", "iat", "exp", "jti", "iss"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError(401, "token_expired") from e
        except jwt.InvalidTokenError as e:
            raise AuthError(401, "token_invalid") from e
        return str(claims["sub"])


class Users:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def create(self, username: str, password: str, role: Role) -> UserRow:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        if len(password) < MIN_PASSWORD_CHARS:
            raise ValueError(f"passwords need at least {MIN_PASSWORD_CHARS} characters")
        user = UserRow(id=str(uuid.uuid4()), username=username, role=role, password_hash=hash_password(password))
        async with self.sessions.begin() as s:
            s.add(user)
        return user

    async def ensure(self, username: str, password: str, role: Role) -> UserRow:
        """Create the user unless the username exists (demo seeding is idempotent)."""
        async with self.sessions() as s:
            existing = (await s.execute(select(UserRow).where(UserRow.username == username))).scalar_one_or_none()
        return existing or await self.create(username, password, role)

    async def authenticate(self, username: str, password: str) -> UserRow:
        async with self.sessions() as s:
            user = (await s.execute(select(UserRow).where(UserRow.username == username))).scalar_one_or_none()
        if user is None:
            check_password(password, _DUMMY_HASH)
            raise AuthError(401, "unknown_user")
        if not check_password(password, user.password_hash):
            raise AuthError(401, "bad_password")
        if not user.active:
            raise AuthError(401, "inactive_user")
        return user

    async def principal(self, user_id: str) -> Principal:
        async with self.sessions() as s:
            user = await s.get(UserRow, user_id)
        if user is None or not user.active or user.role not in ROLES:
            raise AuthError(401, "inactive_user")
        return Principal(user_id=user.id, username=user.username, role=user.role)  # narrowed to Role by the check above

    async def set_active(self, user_id: str, active: bool) -> None:
        async with self.sessions.begin() as s:
            user = await s.get(UserRow, user_id)
            if user is not None:
                user.active = active


DEMO_USERS: tuple[tuple[str, Role], ...] = (
    ("demo-system", "system"),
    ("demo-reviewer", "reviewer"),
    ("demo-admin", "admin"),
)
