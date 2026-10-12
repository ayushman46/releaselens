"""``python -m rl_api.admin``: bootstrap an org and a key, then use the key."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from rl_api.admin import main
from rl_api.app import create_app
from rl_api.settings import Settings

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
