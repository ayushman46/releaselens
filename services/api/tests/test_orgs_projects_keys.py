"""Organizations, members, invitations, projects/services/environments, plan limits and
API key management, with the audit entries each action leaves (M10)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from rl_api.testing import Harness, UserClient, saas_settings, unique_email
from rl_db.models import Invitation, Organization, Plan

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_orgs")


@pytest.fixture
async def h(db_url: str) -> AsyncIterator[Harness]:
    harness = Harness(saas_settings(db_url))
    async with harness.running():
        yield harness


async def _audit(owner: UserClient, org_id: uuid.UUID, **params: Any) -> list[dict[str, Any]]:
    resp = await owner.get(f"/api/v1/orgs/{org_id}/audit-log", params=params)
    assert resp.status_code == 200, resp.text
    items: list[dict[str, Any]] = resp.json()["items"]
    return items


# ----------------------------------------------------------------------------- organizations
async def test_create_org_makes_the_creator_owner_and_slugs_are_unique(h: Harness) -> None:
    owner = await h.signup(unique_email("o"))
    slug = f"acme-{uuid.uuid4().hex[:8]}"
    resp = await owner.post("/api/v1/orgs", json={"name": "Acme", "slug": slug})
    assert resp.status_code == 201
    org = resp.json()
    assert (org["role"], org["plan"]) == ("owner", "free")
    assert org["limits"]["max_projects"] == 1
    dup = await owner.post("/api/v1/orgs", json={"name": "Other", "slug": slug})
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "slug_taken"
    bad = await owner.post("/api/v1/orgs", json={"name": "X", "slug": "Not A Slug"})
    assert bad.status_code == 422
    entries = await _audit(owner, uuid.UUID(org["id"]), action="org.created")
    assert entries[0]["actor_email"] == owner.email


async def test_last_owner_rules_and_owner_only_grants(h: Harness) -> None:
    owner = await h.signup(unique_email("o"))
    org_id = await h.create_org(owner)
    admin = await h.add_member(owner, org_id, "admin")
    member = await h.add_member(owner, org_id, "member")
    base = f"/api/v1/orgs/{org_id}/members"

    # An admin cannot touch the owner role in either direction.
    resp = await admin.patch(f"{base}/{member.user_id}", json={"role": "owner"})
    assert resp.status_code == 403
    resp = await admin.patch(f"{base}/{owner.user_id}", json={"role": "admin"})
    assert resp.status_code == 403
    resp = await admin.delete(f"{base}/{owner.user_id}")
    assert resp.status_code == 403
    # The only owner can neither demote nor remove themselves.
    resp = await owner.patch(f"{base}/{owner.user_id}", json={"role": "admin"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "last_owner"
    assert (await owner.delete(f"{base}/{owner.user_id}")).status_code == 409
    # With a second owner, the first may step down.
    assert (await owner.patch(f"{base}/{admin.user_id}", json={"role": "owner"})).status_code == 200
    resp = await owner.patch(f"{base}/{owner.user_id}", json={"role": "admin"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"
    # Admin changes a member's role; the member may leave on their own.
    new_owner = admin
    resp = await new_owner.patch(f"{base}/{member.user_id}", json={"role": "viewer"})
    assert resp.json()["role"] == "viewer"
    assert (await member.delete(f"{base}/{member.user_id}")).status_code == 204
    assert (await member.get(f"/api/v1/orgs/{org_id}")).status_code == 404  # gone at once

    changes = await _audit(new_owner, org_id, action="member.role_changed")
    assert {(e["metadata"]["from"], e["metadata"]["to"]) for e in changes} >= {
        ("admin", "owner"),
        ("owner", "admin"),
        ("member", "viewer"),
    }
    removed = await _audit(new_owner, org_id, action="member.removed")
    assert removed[0]["target_id"] == str(member.user_id)
    assert removed[0]["metadata"]["self"] is True


async def test_owner_deletes_org_and_the_audit_log_survives(h: Harness) -> None:
    owner = await h.signup(unique_email("o"))
    org_id = await h.create_org(owner)
    assert (await owner.delete(f"/api/v1/orgs/{org_id}")).status_code == 204
    assert (await owner.get(f"/api/v1/orgs/{org_id}")).status_code == 404
    from sqlalchemy import select

    from rl_db.models import AuditLogEntry

    async with h.app.state.db_sessions() as s:
        actions = set(
            (
                await s.execute(
                    select(AuditLogEntry.action).where(AuditLogEntry.tenant_id == org_id)
                )
            ).scalars()
        )
    assert {"org.created", "org.deleted"} <= actions


async def test_org_with_live_subscription_cannot_be_deleted(h: Harness) -> None:
    from rl_db.admin.billing import SubscriptionState, apply_subscription_state

    owner = await h.signup(unique_email("o"))
    org_id = await h.create_org(owner)
    async with h.app.state.db_sessions.begin() as s:
        await apply_subscription_state(
            s,
            SubscriptionState(
                tenant_id=org_id,
                customer_id=f"cus_{uuid.uuid4().hex[:10]}",
                subscription_id=None,
                status="active",
                plan=Plan.TEAM,
                price_id=None,
                current_period_end=None,
                cancel_at_period_end=False,
                event_at=datetime.now(UTC),
            ),
        )
    resp = await owner.delete(f"/api/v1/orgs/{org_id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "subscription_active"


# ----------------------------------------------------------------------------- invitations
async def test_invitations_are_bound_to_the_address_single_use_and_expire(h: Harness) -> None:
    owner = await h.signup(unique_email("o"))
    org_id = await h.create_org(owner)
    invited = unique_email("invited")
    resp = await owner.post(
        f"/api/v1/orgs/{org_id}/invites", json={"email": invited, "role": "member"}
    )
    assert resp.status_code == 201
    assert "token" not in resp.json()  # only e-mailed
    token = h.token_from_mail("invitation", invited)

    stranger = await h.signup(unique_email("stranger"))
    wrong = await stranger.post("/api/v1/invites/accept", json={"token": token})
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "invite_email_mismatch"

    invitee = await h.signup(invited)
    ok = await invitee.post("/api/v1/invites/accept", json={"token": token})
    assert ok.status_code == 200
    assert ok.json() == {"org_id": str(org_id), "role": "member"}
    again = await invitee.post("/api/v1/invites/accept", json={"token": token})
    assert again.status_code == 400

    dup = await owner.post(
        f"/api/v1/orgs/{org_id}/invites", json={"email": invited, "role": "viewer"}
    )
    assert dup.status_code == 409

    late = unique_email("late")
    await owner.post(f"/api/v1/orgs/{org_id}/invites", json={"email": late, "role": "viewer"})
    late_token = h.token_from_mail("invitation", late)
    async with h.app.state.db_sessions.begin() as s:
        await s.execute(
            update(Invitation)
            .where(Invitation.email == late)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    late_user = await h.signup(late)
    assert (
        await late_user.post("/api/v1/invites/accept", json={"token": late_token})
    ).status_code == 400

    entries = await _audit(owner, org_id, action="member.invited")
    assert {e["metadata"]["email"] for e in entries} >= {invited, late}
    joined = await _audit(owner, org_id, action="member.joined")
    assert joined[0]["actor_user_id"] == str(invitee.user_id)


async def test_admin_cannot_invite_an_owner_and_invites_can_be_withdrawn(h: Harness) -> None:
    owner = await h.signup(unique_email("o"))
    org_id = await h.create_org(owner)
    admin = await h.add_member(owner, org_id, "admin")
    base = f"/api/v1/orgs/{org_id}/invites"
    assert (
        await admin.post(base, json={"email": unique_email(), "role": "owner"})
    ).status_code == 403
    email = unique_email("w")
    invite = (await admin.post(base, json={"email": email, "role": "viewer"})).json()
    token = h.token_from_mail("invitation", email)
    assert [i["id"] for i in (await admin.get(base)).json()] == [invite["id"]]
    assert (await admin.delete(f"{base}/{invite['id']}")).status_code == 204
    assert (await admin.get(base)).json() == []
    user = await h.signup(email)
    assert (await user.post("/api/v1/invites/accept", json={"token": token})).status_code == 400


# ----------------------------------------------------------------------------- catalog + limits
async def test_projects_services_environments_crud_and_free_plan_limits(h: Harness) -> None:
    owner = await h.signup(unique_email("o"))
    org_id = await h.create_org(owner)
    member = await h.add_member(owner, org_id, "member")
    base = f"/api/v1/orgs/{org_id}/projects"
    created = await member.post(
        base,
        json={
            "name": "Payments",
            "slug": "payments",
            "github": {"owner": "acme", "repo": "payments", "installation_id": 77},
        },
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["github"] == {"owner": "acme", "repo": "payments", "installation_id": 77}
    pid = project["id"]

    second = await member.post(base, json={"name": "Second", "slug": "second"})
    assert second.status_code == 402
    err = second.json()["error"]
    assert err["code"] == "plan_limit_reached"
    assert err["details"]["limit"] == "projects"
    assert (err["details"]["max"], err["details"]["current"]) == (1, 1)
    assert err["details"]["upgrade"]["plan"] == "team"

    for i in range(3):
        resp = await member.post(f"{base}/{pid}/services", json={"name": f"svc-{i}"})
        assert resp.status_code == 201, resp.text
    over = await member.post(f"{base}/{pid}/services", json={"name": "svc-3"})
    assert over.status_code == 402
    assert over.json()["error"]["details"]["limit"] == "services"

    dup_env = None
    for name in ("production", "production"):
        dup_env = await member.post(f"{base}/{pid}/environments", json={"name": name})
    assert dup_env is not None
    assert dup_env.status_code == 409
    envs = (await member.get(f"{base}/{pid}/environments")).json()
    assert [e["name"] for e in envs] == ["production"]

    disconnected = await member.patch(f"{base}/{pid}", json={"github": None})
    assert disconnected.json()["github"] is None
    assert (await member.delete(f"{base}/{pid}")).status_code == 403  # admin+
    assert (await owner.delete(f"{base}/{pid}")).status_code == 204
    assert (await owner.get(f"{base}/{pid}/services")).status_code == 404

    # Upgraded to Team, the second project fits.
    async with h.app.state.db_sessions.begin() as s:
        await s.execute(
            update(Organization).where(Organization.id == org_id).values(plan=Plan.TEAM)
        )
    assert (await member.post(base, json={"name": "A", "slug": "a"})).status_code == 201
    assert (await member.post(base, json={"name": "B", "slug": "b"})).status_code == 201


async def test_service_names_are_unique_per_organization(h: Harness) -> None:
    owner = await h.signup(unique_email("o"))
    org_id = await h.create_org(owner)
    async with h.app.state.db_sessions.begin() as s:
        await s.execute(
            update(Organization).where(Organization.id == org_id).values(plan=Plan.TEAM)
        )
    base = f"/api/v1/orgs/{org_id}/projects"
    p1 = (await owner.post(base, json={"name": "One", "slug": "one"})).json()["id"]
    p2 = (await owner.post(base, json={"name": "Two", "slug": "two"})).json()["id"]
    assert (await owner.post(f"{base}/{p1}/services", json={"name": "api"})).status_code == 201
    clash = await owner.post(f"{base}/{p2}/services", json={"name": "api"})
    assert clash.status_code == 409
    # A service id is only reachable through its own project.
    sid = (await owner.get(f"{base}/{p1}/services")).json()[0]["id"]
    assert (await owner.get(f"{base}/{p2}/services/{sid}")).status_code == 404


# ----------------------------------------------------------------------------- API keys
async def test_api_key_lifecycle_create_once_list_rotate_revoke(h: Harness) -> None:
    owner = await h.signup(unique_email("o"))
    org_id = await h.create_org(owner)
    admin = await h.add_member(owner, org_id, "admin")
    base = f"/api/v1/orgs/{org_id}/api-keys"
    created = await admin.post(
        base, json={"name": "ci", "scopes": ["read", "deployments:write", "read"]}
    )
    assert created.status_code == 201, created.text
    key = created.json()
    plaintext = key["key"]
    assert plaintext.startswith(f"rlk_{key['prefix']}_")
    assert key["scopes"] == ["deployments:write", "read"]
    assert key["created_by"] == str(admin.user_id)

    listed = (await admin.get(base)).json()
    assert len(listed) == 1
    assert "key" not in listed[0]
    assert set(listed[0]) >= {
        "prefix",
        "name",
        "scopes",
        "created_by",
        "last_used_at",
        "expires_at",
    }

    caller = h.client()
    projects = f"/api/v1/orgs/{org_id}/projects"
    assert (await caller.get(projects, headers={"x-releaselens-key": plaintext})).status_code == 200

    rotated = await admin.post(f"{base}/{key['id']}/rotate")
    assert rotated.status_code == 201
    new = rotated.json()
    assert new["key"] != plaintext
    assert (new["name"], new["scopes"]) == ("ci", key["scopes"])
    assert (await caller.get(projects, headers={"x-releaselens-key": plaintext})).status_code == 401
    assert (
        await caller.get(projects, headers={"x-releaselens-key": new["key"]})
    ).status_code == 200
    assert (await admin.post(f"{base}/{key['id']}/rotate")).status_code == 409

    assert (await admin.delete(f"{base}/{new['id']}")).status_code == 204
    assert (await admin.delete(f"{base}/{new['id']}")).status_code == 204  # idempotent
    assert (
        await caller.get(projects, headers={"x-releaselens-key": new["key"]})
    ).status_code == 401

    bad = await admin.post(base, json={"name": "x", "scopes": ["admin:everything"]})
    assert bad.status_code == 422
    past = await admin.post(
        base,
        json={"name": "x", "scopes": ["read"], "expires_at": "2020-01-01T00:00:00Z"},
    )
    assert past.status_code == 422

    actions = [e["action"] for e in await _audit(owner, org_id, target_type="api_key")]
    assert actions.count("api_key.created") == 1
    assert actions.count("api_key.rotated") == 1
    assert actions.count("api_key.revoked") == 1
    for entry in await _audit(owner, org_id, target_type="api_key"):
        assert plaintext not in str(entry)
        assert new["key"] not in str(entry)


async def test_revoked_key_stops_ingest_immediately(h: Harness) -> None:
    owner = await h.signup(unique_email("o"))
    org_id = await h.create_org(owner)
    key = (
        await owner.post(
            f"/api/v1/orgs/{org_id}/api-keys", json={"name": "cd", "scopes": ["deployments:write"]}
        )
    ).json()
    await owner.delete(f"/api/v1/orgs/{org_id}/api-keys/{key['id']}")
    resp = await h.client().post(
        "/api/v1/deployments",
        headers={"x-releaselens-key": key["key"]},
        json={
            "service": "payment",
            "environment": "production",
            "version": "v2",
            "commit_sha": "abcdef1",
            "deployed_at": datetime.now(UTC).isoformat(),
        },
    )
    assert resp.status_code == 401
