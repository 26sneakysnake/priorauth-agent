"""Authentication (passwords, JWT) and role-based access control, route by route."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from sqlalchemy.exc import IntegrityError

from priorauth.api.fhir import case_to_bundle
from priorauth.jobs.db import create_schema, make_engine, sessions
from priorauth.security.auth import AuthError, TokenIssuer, UserRow, Users, check_password, hash_password
from tests.test_api import DEMO_PASSWORD, DEV, JWT_SECRET, api, login, post, wait_done

SECRET = JWT_SECRET.encode()


def user(role: str = "reviewer") -> UserRow:
    return UserRow(id="7d1d5a0e-0000-4000-8000-000000000001", username="u", role=role, password_hash="x")


def test_password_hashes_are_salted_and_checked() -> None:
    first, second = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert first != second and first.startswith("scrypt$")
    assert check_password("correct horse battery", first) and check_password("correct horse battery", second)
    assert not check_password("correct horse batterY", first)
    assert not check_password("anything", "md5$abc") and not check_password("anything", "garbage")


def test_tokens_carry_the_claims_and_expire() -> None:
    issuer = TokenIssuer(SECRET)
    token = issuer.issue(user())
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], issuer="priorauth-agent")
    assert set(claims) == {"sub", "role", "iat", "exp", "jti", "iss"}
    assert claims["exp"] - claims["iat"] == 15 * 60
    assert issuer.subject(token) == user().id
    assert issuer.issue(user()) != token  # unique jti
    stale = TokenIssuer(SECRET, clock=lambda: datetime.now(UTC) - timedelta(minutes=16))
    with pytest.raises(AuthError) as e:
        issuer.subject(stale.issue(user()))
    assert (e.value.status, e.value.reason) == (401, "token_expired")


def _forge(claims: dict[str, Any], alg: str = "none") -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': alg, 'typ': 'JWT'})}.{part(claims)}."


def test_forged_tampered_and_foreign_tokens_are_rejected() -> None:
    issuer = TokenIssuer(SECRET)
    token = issuer.issue(user("reviewer"))
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    elevated = base64.urlsafe_b64encode(json.dumps(claims | {"role": "admin"}).encode()).rstrip(b"=").decode()
    now = int(datetime.now(UTC).timestamp())
    bad = [
        f"{header}.{elevated}.{signature}",  # payload changed, signature kept
        _forge(claims),  # alg=none
        TokenIssuer(b"another-secret-of-at-least-32-bytes!").issue(user()),  # other key
        jwt.encode({"sub": "x", "iat": now, "exp": now + 60}, SECRET, algorithm="HS256"),  # missing claims
        jwt.encode(claims | {"iss": "someone-else"}, SECRET, algorithm="HS256"),  # other issuer
        "not-a-jwt",
    ]
    for forged in bad:
        with pytest.raises(AuthError) as e:
            issuer.subject(forged)
        assert e.value.status == 401


def test_short_jwt_secrets_are_refused() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        TokenIssuer(b"short")


def test_users_have_unique_ids_and_usernames(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
        await create_schema(engine)
        users = Users(sessions(engine))
        a = await users.create("alice", "long enough password", "reviewer")
        b = await users.create("bob", "long enough password", "reviewer")
        assert a.id != b.id and len(a.id) == 36
        with pytest.raises(IntegrityError):
            await users.create("alice", "another long password", "admin")
        with pytest.raises(ValueError, match="at least 12"):
            await users.create("carol", "short", "admin")
        with pytest.raises(ValueError, match="unknown role"):
            await users.create("dave", "long enough password", "superuser")  # type: ignore[arg-type]
        await engine.dispose()

    asyncio.run(scenario())


# Route -> roles allowed. "public" routes need no token.
CASE = "{case_id}"
MATRIX: list[tuple[str, str, set[str | None]]] = [
    ("GET", "/health", {None, "system", "reviewer", "admin"}),
    ("POST", "/cases", {"system", "admin"}),
    ("GET", f"/cases/{CASE}", {"system", "reviewer", "admin"}),
    ("GET", f"/cases/{CASE}/result", {"system", "reviewer", "admin"}),
    ("GET", "/auth/me", {"system", "reviewer", "admin"}),
    ("GET", "/costs/daily", {"admin"}),
    ("GET", "/audit", {"admin"}),
    ("GET", "/audit/verify", {"admin"}),
]
ROLES: list[str | None] = [None, "system", "reviewer", "admin"]


def test_every_route_enforces_its_roles(tmp_path: Path) -> None:
    """The full matrix: each route called with no token and with a token of each role."""

    async def scenario() -> None:
        async with api(tmp_path, role=None) as (client, _):
            tokens = {r: await login(client, f"demo-{r}") for r in ("system", "reviewer", "admin")}
            submitted = await client.post(
                "/cases",
                json=case_to_bundle(DEV[0][0]),
                headers={"Idempotency-Key": "matrix", "Authorization": f"Bearer {tokens['system']}"},
            )
            case_id = submitted.json()["case_id"]
            await wait_done(client, case_id, headers={"Authorization": f"Bearer {tokens['admin']}"})
            bundle = case_to_bundle(DEV[1][0])
            for method, template, allowed in MATRIX:
                for role in ROLES:
                    headers = {"Idempotency-Key": f"matrix-{role}"}
                    if role is not None:
                        headers["Authorization"] = f"Bearer {tokens[role]}"
                    url = template.format(case_id=case_id)
                    r = await client.request(method, url, headers=headers, json=bundle if method == "POST" else None)
                    if role in allowed:
                        assert r.status_code in (200, 202), (method, url, role, r.text)
                    elif role is None:
                        assert r.status_code == 401, (method, url)
                        assert r.headers["www-authenticate"] == "Bearer"
                    else:
                        assert r.status_code == 403, (method, url, role)

    asyncio.run(scenario())


def test_login_failures_are_401_whatever_the_cause(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with api(tmp_path, role=None, run_workers=False) as (client, _):
            for username, password in (("demo-admin", "wrong password!"), ("nobody", DEMO_PASSWORD)):
                r = await client.post("/auth/token", json={"username": username, "password": password})
                assert r.status_code == 401 and r.json() == {"detail": "invalid credentials"}
            r = await client.get("/cases/x", headers={"Authorization": "Bearer not-a-token"})
            assert r.status_code == 401

    asyncio.run(scenario())


def test_a_deactivated_user_is_refused_at_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with api(tmp_path, role="reviewer", run_workers=False) as (client, _):
            assert (await client.get("/auth/me")).status_code == 200
            app_users: Users = client._transport.app.state.users  # type: ignore[attr-defined]
            me = (await client.get("/auth/me")).json()
            await app_users.set_active(me["user_id"], False)
            assert (await client.get("/auth/me")).status_code == 401  # same, still unexpired token

    asyncio.run(scenario())


def test_a_system_user_only_sees_the_cases_it_submitted(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with api(tmp_path, role="system", run_workers=False) as (client, _):
            mine = (await post(client, case_to_bundle(DEV[0][0]), key="mine")).json()["case_id"]
            app_users: Users = client._transport.app.state.users  # type: ignore[attr-defined]
            await app_users.create("other-system", "other system password", "system")
            other = await login(client, "other-system", "other system password")
            as_other = {"Authorization": f"Bearer {other}"}
            assert (await client.get(f"/cases/{mine}")).status_code == 200
            assert (await client.get(f"/cases/{mine}", headers=as_other)).status_code == 404
            assert (await client.get(f"/cases/{mine}/result", headers=as_other)).status_code == 404
            reviewer = {"Authorization": f"Bearer {await login(client, 'demo-reviewer')}"}
            assert (await client.get(f"/cases/{mine}", headers=reviewer)).status_code == 200

    asyncio.run(scenario())
