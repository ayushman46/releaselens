"""``python -m rl_api.admin``: bootstrap an org and a key, then use the key."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rl_api.admin import main
from rl_api.app import create_app
from rl_api.settings import Settings
from rl_db import create_engine, create_session_factory
from rl_db.admin.accounts import create_user, get_membership_role
from rl_db.models import AuditLogEntry, Role

pytestmark = pytest.mark.integration


def test_create_org_then_key_then_authenticate(
    api_db_url: str, kafka_bootstrap: str, capsys: pytest.CaptureFixture[str]
) -> None:
    slug = f"cli-{uuid.uuid4().hex[:8]}"
    assert (
        main(["--database-url", api_db_url, "create-org", "--name", "CLI Org", "--slug", slug]) == 0
    )
    org_id = uuid.UUID(capsys.readouterr().out.strip())

    # Duplicate slug: clean error, non-zero exit, nothing on stdout.
    assert main(["--database-url", api_db_url, "create-org", "--name", "x", "--slug", slug]) == 1
    dup = capsys.readouterr()
    assert dup.out == ""
    assert "slug already exists" in dup.err

    code = main(
        [
            "--database-url",
            api_db_url,
            "create-key",
            "--org",
            slug,
            "--scopes",
            "deployments:write,read",
            "--name",
            "ci",
            "--expires-in-days",
            "30",
        ]
    )
    out = capsys.readouterr()
    assert code == 0
    key = out.out.strip()
    assert key.startswith("rlk_")
    assert len(key) == 60
    assert "only time the key is shown" in out.err
    assert key not in out.err  # the plaintext appears exactly once, on stdout

    settings = Settings(
        env="test", database_url=api_db_url, kafka_bootstrap=kafka_bootstrap, otel_enabled=False
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/api/v1/deployments", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "next_cursor": None}
    assert org_id.version == 7


def test_create_key_errors(api_db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["--database-url", api_db_url, "create-key", "--org", "nope", "--scopes", "read"]) == 2
    )
    assert "no organization" in capsys.readouterr().err

    slug = f"cli-{uuid.uuid4().hex[:8]}"
    assert main(["--database-url", api_db_url, "create-org", "--name", "x", "--slug", slug]) == 0
    capsys.readouterr()
    assert (
        main(["--database-url", api_db_url, "create-key", "--org", slug, "--scopes", "admin"]) == 2
    )
    err = capsys.readouterr()
    assert "unknown scope" in err.err
    assert err.out == ""


def test_link_github_installation(api_db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    slugs = [f"gh-{uuid.uuid4().hex[:8]}" for _ in range(2)]
    for slug in slugs:
        assert (
            main(["--database-url", api_db_url, "create-org", "--name", "G", "--slug", slug]) == 0
        )
    capsys.readouterr()
    inst = str(int(uuid.uuid4().int % 10**9) + 1)
    link = ["--database-url", api_db_url, "link-github-installation", "--installation-id", inst]
    assert main([*link, "--org", slugs[0], "--account", "Acme"]) == 0
    assert "linked to" in capsys.readouterr().err
    assert main([*link, "--org", slugs[0], "--account", "acme"]) == 0  # idempotent
    capsys.readouterr()
    assert main([*link, "--org", slugs[1], "--account", "acme"]) == 1  # owned by another org
    assert "another organization" in capsys.readouterr().err
    assert main([*link, "--org", "no-such-org", "--account", "acme"]) == 2


def test_add_member_gives_an_existing_user_a_role(
    api_db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    slug = f"cli-{uuid.uuid4().hex[:8]}"
    email = f"Member-{uuid.uuid4().hex[:8]}@Example.com"
    assert main(["--database-url", api_db_url, "create-org", "--name", "M", "--slug", slug]) == 0
    org_id = uuid.UUID(capsys.readouterr().out.strip())

    def add(role: str, who: str = email, org: str = slug) -> int:
        argv = ["--database-url", api_db_url, "add-member", "--org", org, "--email", who]
        return main([*argv, "--role", role])

    # The user must exist: the CLI never creates one.
    assert add("owner") == 2
    assert "no user" in capsys.readouterr().err

    async def make_user() -> uuid.UUID:
        engine = create_engine(api_db_url, pool_size=1)
        try:
            async with create_session_factory(engine).begin() as s:
                user = await create_user(s, email=email, password_hash=None)
            return user.id
        finally:
            await engine.dispose()

    user_id = asyncio.run(make_user())

    async def state() -> tuple[Role | None, list[str]]:
        engine = create_engine(api_db_url, pool_size=1)
        try:
            async with create_session_factory(engine)() as s:
                role = await get_membership_role(s, tenant_id=org_id, user_id=user_id)
                actions = (
                    await s.execute(
                        select(AuditLogEntry.action)
                        .where(AuditLogEntry.tenant_id == org_id)
                        .order_by(AuditLogEntry.created_at, AuditLogEntry.id)
                    )
                ).scalars()
                return role, list(actions)
        finally:
            await engine.dispose()

    assert add("owner", who=email.upper()) == 0  # e-mail lookup is case-insensitive
    assert "member added" in capsys.readouterr().err
    assert asyncio.run(state()) == (Role.OWNER, ["member.joined"])
    assert add("owner") == 0  # idempotent
    assert "unchanged" in capsys.readouterr().err
    assert add("viewer") == 0
    assert "changed from owner" in capsys.readouterr().err
    assert asyncio.run(state()) == (Role.VIEWER, ["member.joined", "member.role_changed"])

    assert add("viewer", org="no-such-org") == 2
    assert "no organization" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        add("superuser")
