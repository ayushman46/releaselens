"""Cross-tenant isolation and the RBAC matrix for EVERY organization-scoped endpoint.

One world is built per module: organization A (owner, admin, member, viewer; a project
with a service, an environment, an alert rule; API keys; a pending invitation) and
organization B (owner, the same resources, a read key). Then, for every route under
``/api/v1/orgs/{org_id}``:

* B's owner, B's API key and B's owner pointing at A's *resource* ids from B's own org
  all get **404** (existence never leaks), and nothing of A changes;
* every A role below the route's minimum gets **403**; read routes answer 200 to every
  allowed role and to a ``read`` API key; session-only routes refuse API keys (403);
* no credentials, revoked keys and expired keys get **401**.

``ROUTES`` must list every org-scoped route: a new route that is not added here fails
``test_matrix_covers_every_org_route``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from rl_api.apikeys import create_api_key
from rl_api.testing import Harness, UserClient, saas_settings, unique_email
from rl_core.tenancy import Principal, TenantContext
from rl_db.repositories import revoke_api_key

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

VIEWER, MEMBER, ADMIN, OWNER = "viewer", "member", "admin", "owner"
RANK = {VIEWER: 0, MEMBER: 1, ADMIN: 2, OWNER: 3}


@dataclass(frozen=True)
class Route:
    method: str
    path: str  # template with {org_id}, {project_id}, …
    min_role: str
    key_ok: bool = False  # a ``read`` API key of the org may call it
    body: dict[str, Any] | None = None


P = "/api/v1/orgs/{org_id}"
PR = P + "/projects/{project_id}"
ROUTES: list[Route] = [
    Route("GET", P, VIEWER, key_ok=True),
    Route("PATCH", P, ADMIN, body={"name": "Renamed"}),
    Route("DELETE", P, OWNER),
    Route("GET", P + "/members", VIEWER),
    Route("PATCH", P + "/members/{user_id}", ADMIN, body={"role": "viewer"}),
    Route("DELETE", P + "/members/{user_id}", ADMIN),
    Route("POST", P + "/invites", ADMIN, body={"email": "new@example.com", "role": "member"}),
    Route("GET", P + "/invites", ADMIN),
    Route("DELETE", P + "/invites/{invite_id}", ADMIN),
    Route("GET", P + "/projects", VIEWER, key_ok=True),
    Route("POST", P + "/projects", MEMBER, body={"name": "Another", "slug": "another"}),
    Route("GET", PR, VIEWER, key_ok=True),
    Route("PATCH", PR, MEMBER, body={"name": "Renamed project"}),
    Route("DELETE", PR, ADMIN),
    Route("GET", PR + "/services", VIEWER, key_ok=True),
    Route("POST", PR + "/services", MEMBER, body={"name": "svc-new"}),
    Route("GET", PR + "/services/{service_id}", VIEWER, key_ok=True),
    Route("PATCH", PR + "/services/{service_id}", MEMBER, body={"description": "changed"}),
    Route("DELETE", PR + "/services/{service_id}", MEMBER),
    Route("GET", PR + "/environments", VIEWER, key_ok=True),
    Route("POST", PR + "/environments", MEMBER, body={"name": "staging"}),
    Route("PATCH", PR + "/environments/{environment_id}", MEMBER, body={"name": "qa"}),
    Route("DELETE", PR + "/environments/{environment_id}", MEMBER),
    Route("GET", PR + "/alert-rules", VIEWER, key_ok=True),
    Route(
        "POST",
        PR + "/alert-rules",
        ADMIN,
        body={"name": "r", "email_recipients": ["a@example.com"]},
    ),
    Route("GET", PR + "/alert-rules/{rule_id}", VIEWER, key_ok=True),
    Route("PATCH", PR + "/alert-rules/{rule_id}", ADMIN, body={"enabled": False}),
    Route("DELETE", PR + "/alert-rules/{rule_id}", ADMIN),
    Route("GET", P + "/notifications", VIEWER),
    Route("GET", P + "/api-keys", ADMIN),
    Route("POST", P + "/api-keys", ADMIN, body={"name": "k", "scopes": ["read"]}),
    Route("DELETE", P + "/api-keys/{key_id}", ADMIN),
    Route("POST", P + "/api-keys/{key_id}/rotate", ADMIN),
    Route("GET", P + "/audit-log", ADMIN),
    Route("GET", P + "/audit-log/export.csv", ADMIN),
    Route("GET", P + "/billing", VIEWER),
    Route("POST", P + "/billing/checkout", OWNER, body={"plan": "team"}),
    Route("POST", P + "/billing/portal", OWNER),
]


def _has_resource_ids(route: Route) -> bool:
    return "{" in route.path.removeprefix(P)


def _rid(route: Route) -> str:
    return f"{route.method} {route.path.removeprefix('/api/v1/orgs/{org_id}') or '/'}"


@dataclass
class Org:
    id: uuid.UUID
    users: dict[str, UserClient] = field(default_factory=dict)
    ids: dict[str, str] = field(default_factory=dict)  # path parameter values
    read_key: str = ""
    revoked_key: str = ""
    expired_key: str = ""

    def url(self, route: Route, *, resources: Org | None = None) -> str:
        """Fill the template: org id from this org, resource ids from ``resources``."""
        values = {"org_id": str(self.id), **(resources or self).ids}
        return route.path.format(**values)


@dataclass
class World:
    h: Harness
    a: Org
    b: Org


async def _build_org(h: Harness, label: str, roles: list[str]) -> Org:
    owner = await h.signup(unique_email(f"{label}-owner"))
    org = Org(id=await h.create_org(owner, f"{label}-{uuid.uuid4().hex[:8]}"))
    org.users[OWNER] = owner
    for role in roles:
        org.users[role] = await h.add_member(owner, org.id, role)
    base = f"/api/v1/orgs/{org.id}"
    project = (await owner.post(f"{base}/projects", json={"name": "Web", "slug": "web"})).json()
    pid = project["id"]
    service = (
        await owner.post(f"{base}/projects/{pid}/services", json={"name": f"svc-{label}"})
    ).json()
    env = (
        await owner.post(f"{base}/projects/{pid}/environments", json={"name": "production"})
    ).json()
    rule = (
        await owner.post(
            f"{base}/projects/{pid}/alert-rules",
            json={"name": "on-call", "email_recipients": [f"oncall-{label}@example.com"]},
        )
    ).json()
    key = (await owner.post(f"{base}/api-keys", json={"name": "ci", "scopes": ["read"]})).json()
    invite = (
        await owner.post(
            f"{base}/invites", json={"email": unique_email("pending"), "role": "viewer"}
        )
    ).json()
    # Member routes target the owner: a lower role acting on itself would be "leaving",
    # which is allowed and would change the world.
    target = owner
    org.ids = {
        "project_id": pid,
        "service_id": service["id"],
        "environment_id": env["id"],
        "rule_id": rule["id"],
        "key_id": key["id"],
        "invite_id": invite["id"],
        "user_id": str(target.user_id),
    }
    org.read_key = key["key"]

    async with h.app.state.db_sessions.begin() as s:
        revoked = await create_api_key(s, org.id, "revoked", ["read"])
        expired = await create_api_key(
            s, org.id, "expired", ["read"], expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        await revoke_api_key(
            s, TenantContext(tenant_id=org.id, principal=Principal.SYSTEM), revoked.id
        )
    org.revoked_key, org.expired_key = revoked.plaintext, expired.plaintext
    return org


@pytest.fixture(scope="module")
def db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_matrix")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world(db_url: str) -> AsyncIterator[World]:
    h = Harness(saas_settings(db_url))
    async with h.running():
        a = await _build_org(h, "a", [ADMIN, MEMBER, VIEWER])
        b = await _build_org(h, "b", [MEMBER])
        yield World(h=h, a=a, b=b)


async def _call(client: UserClient, route: Route, url: str, **kw: Any) -> Any:
    return await client.request(route.method, url, json=route.body, **kw)


async def _snapshot(w: World) -> dict[str, Any]:
    owner, base = w.a.users[OWNER], f"/api/v1/orgs/{w.a.id}"
    pid = w.a.ids["project_id"]
    return {
        "org": (await owner.get(base)).json(),
        "members": (await owner.get(f"{base}/members")).json(),
        "projects": (await owner.get(f"{base}/projects")).json(),
        "services": (await owner.get(f"{base}/projects/{pid}/services")).json(),
        "envs": (await owner.get(f"{base}/projects/{pid}/environments")).json(),
        "rules": (await owner.get(f"{base}/projects/{pid}/alert-rules")).json(),
        "keys": (await owner.get(f"{base}/api-keys")).json(),
        "invites": (await owner.get(f"{base}/invites")).json(),
    }


# ----------------------------------------------------------------------------- coverage
async def test_matrix_covers_every_org_route(world: World) -> None:
    # The OpenAPI document lists every mounted route (FastAPI 0.143 wraps included routers,
    # so app.routes no longer exposes them directly).
    paths = world.h.app.openapi()["paths"]
    app_routes = {
        (method.upper(), path)
        for path, ops in paths.items()
        if path.startswith("/api/v1/orgs/{org_id}")
        for method in ops
    }
    listed = {(r.method, r.path) for r in ROUTES}
    assert app_routes == listed, f"unlisted: {app_routes - listed}, stale: {listed - app_routes}"


# ----------------------------------------------------------------------------- cross-tenant
@pytest.mark.parametrize("route", ROUTES, ids=_rid)
async def test_other_tenants_owner_gets_404(world: World, route: Route) -> None:
    resp = await _call(world.b.users[OWNER], route, world.a.url(route))
    assert resp.status_code == 404, (resp.status_code, resp.text)
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("route", [r for r in ROUTES if _has_resource_ids(r)], ids=_rid)
async def test_own_org_with_other_tenants_resource_ids_gets_404(world: World, route: Route) -> None:
    # B's owner, in B's own org, names A's project/service/rule/key/invite/user ids.
    url = world.b.url(route, resources=world.a)
    resp = await _call(world.b.users[OWNER], route, url)
    assert resp.status_code == 404, (resp.status_code, resp.text)


@pytest.mark.parametrize("route", ROUTES, ids=_rid)
async def test_other_tenants_api_key_gets_404(world: World, route: Route) -> None:
    client = world.h.client()
    resp = await _call(
        client, route, world.a.url(route), headers={"x-releaselens-key": world.b.read_key}
    )
    assert resp.status_code == 404, (resp.status_code, resp.text)


async def test_cross_tenant_attempts_changed_nothing(world: World) -> None:
    before = await _snapshot(world)
    for route in ROUTES:
        await _call(world.b.users[OWNER], route, world.a.url(route))
        if _has_resource_ids(route):
            await _call(world.b.users[OWNER], route, world.b.url(route, resources=world.a))
    assert await _snapshot(world) == before


# ----------------------------------------------------------------------------- RBAC
@pytest.mark.parametrize("route", ROUTES, ids=_rid)
async def test_roles_below_the_minimum_get_403(world: World, route: Route) -> None:
    for role, client in world.a.users.items():
        if RANK[role] >= RANK[route.min_role]:
            continue
        resp = await _call(client, route, world.a.url(route))
        assert resp.status_code == 403, (role, resp.status_code, resp.text)
        assert resp.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("route", [r for r in ROUTES if r.method == "GET"], ids=_rid)
async def test_read_routes_answer_every_allowed_role(world: World, route: Route) -> None:
    for role, client in world.a.users.items():
        resp = await client.get(world.a.url(route))
        expected = 200 if RANK[role] >= RANK[route.min_role] else 403
        assert resp.status_code == expected, (role, resp.status_code, resp.text)


@pytest.mark.parametrize("route", ROUTES, ids=_rid)
async def test_api_keys_only_where_allowed(world: World, route: Route) -> None:
    client = world.h.client()
    resp = await _call(
        client, route, world.a.url(route), headers={"x-releaselens-key": world.a.read_key}
    )
    if route.key_ok:
        assert resp.status_code == 200, resp.text
    else:
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "session_required"


@pytest.mark.parametrize("route", ROUTES, ids=_rid)
async def test_unauthenticated_revoked_and_expired_credentials_get_401(
    world: World, route: Route
) -> None:
    client = world.h.client()
    url = world.a.url(route)
    assert (await _call(client, route, url)).status_code == 401
    for key in (world.a.revoked_key, world.a.expired_key):
        resp = await _call(client, route, url, headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 401, resp.text


async def test_session_org_header_on_tenant_implicit_reads(world: World) -> None:
    viewer, outsider = world.a.users[VIEWER], world.b.users[OWNER]
    ok = await viewer.get("/api/v1/deployments", headers={"X-RL-Org": str(world.a.id)})
    assert ok.status_code == 200
    assert ok.json() == {"items": [], "next_cursor": None}
    foreign = await outsider.get("/api/v1/deployments", headers={"X-RL-Org": str(world.a.id)})
    assert foreign.status_code == 404
    missing = await viewer.get("/api/v1/deployments")
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "org_required"
    garbage = await viewer.get("/api/v1/deployments", headers={"X-RL-Org": "not-a-uuid"})
    assert garbage.status_code == 404
    other = await outsider.get(
        f"/api/v1/deployments/{uuid.uuid4()}", headers={"X-RL-Org": str(world.a.id)}
    )
    assert other.status_code == 404


async def test_my_orgs_lists_only_memberships(world: World) -> None:
    mine = (await world.b.users[OWNER].get("/api/v1/orgs")).json()
    assert [o["id"] for o in mine] == [str(world.b.id)]
    me = (await world.a.users[VIEWER].get("/api/v1/me")).json()
    assert [(m["org_id"], m["role"]) for m in me["memberships"]] == [(str(world.a.id), "viewer")]
