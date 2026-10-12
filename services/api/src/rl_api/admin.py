"""Local bootstrap CLI: create an organization and an API key without a UI (until M10).

::

    python -m rl_api.admin create-org --name "Acme" --slug acme
    python -m rl_api.admin create-key --org acme --scopes deployments:write,read [--name ci]

The database comes from ``--database-url`` or ``RL_DATABASE_URL``. ``create-key`` prints
the key on stdout, alone, so ``KEY=$(python -m rl_api.admin create-key ...)`` captures it.
It is shown exactly once: only its hash is stored. Everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from rl_api.apikeys import KNOWN_SCOPES, create_api_key
from rl_api.settings import Settings
from rl_db import create_engine, create_session_factory
from rl_db.admin.identity import create_organization, get_organization_by_slug


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rl_api.admin", description="Bootstrap organizations and API keys."
    )
    parser.add_argument("--database-url", help="defaults to RL_DATABASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)

    org = sub.add_parser("create-org", help="create an organization (tenant)")
    org.add_argument("--name", required=True)
    org.add_argument("--slug", required=True, help="lowercase letters, digits and dashes")
    org.add_argument("--demo", action="store_true", help="mark as a demo organization")

    key = sub.add_parser("create-key", help="create an API key and print it once")
    key.add_argument("--org", required=True, help="organization slug")
    key.add_argument(
        "--scopes",
        required=True,
        help=f"comma-separated, from: {','.join(sorted(KNOWN_SCOPES))}",
    )
    key.add_argument("--name", default="cli", help="label shown next to the key prefix")
    key.add_argument("--expires-in-days", type=int, help="omit for a non-expiring key")
    return parser


async def _create_org(url: str, name: str, slug: str, demo: bool) -> int:
    engine = create_engine(url, pool_size=1)
    try:
        async with create_session_factory(engine).begin() as session:
            org = await create_organization(session, name=name, slug=slug, is_demo=demo)
    except IntegrityError as exc:
        reason = "slug already exists" if "uq_organizations_slug" in str(exc) else "invalid slug"
        print(f"error: could not create organization: {reason}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"organization created: slug={org.slug} id={org.id}", file=sys.stderr)
    print(org.id)
    return 0


async def _create_key(
    url: str, slug: str, scopes: list[str], name: str, expires_in_days: int | None
) -> int:
    expires_at = None
    if expires_in_days is not None:
        if expires_in_days < 1:
            print("error: --expires-in-days must be at least 1", file=sys.stderr)
            return 2
        expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)
    engine = create_engine(url, pool_size=1)
    try:
        async with create_session_factory(engine).begin() as session:
            org = await get_organization_by_slug(session, slug)
            if org is None:
                print(f"error: no organization with slug {slug!r}", file=sys.stderr)
                return 2
            created = await create_api_key(session, org.id, name, scopes, expires_at=expires_at)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
    print(
        f"API key created: id={created.id} prefix={created.prefix} "
        f"scopes={','.join(sorted(created.scopes))} "
        f"expires_at={created.expires_at.isoformat() if created.expires_at else 'never'}\n"
        "This is the only time the key is shown. Store it now.",
        file=sys.stderr,
    )
    print(created.plaintext)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    url = args.database_url or Settings().database_url
    if args.command == "create-org":
        return asyncio.run(_create_org(url, args.name, args.slug, args.demo))
    scopes = [s for s in args.scopes.split(",") if s.strip()]
    return asyncio.run(_create_key(url, args.org, scopes, args.name, args.expires_in_days))


if __name__ == "__main__":
    sys.exit(main())
