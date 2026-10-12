"""Run Alembic without an ini file: ``python -m rl_db.migrate upgrade head``.

Usable from any working directory (compose ``migrate`` service, tests, Makefile).
"""

from __future__ import annotations

import argparse
import os

from alembic import command
from alembic.config import Config


def make_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "rl_db:migrations")
    resolved = url or os.environ.get("RL_DATABASE_URL")
    if resolved:
        cfg.set_main_option("sqlalchemy.url", resolved)
    return cfg


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rl_db.migrate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upgrade")
    up.add_argument("revision", nargs="?", default="head")
    down = sub.add_parser("downgrade")
    down.add_argument("revision")
    sub.add_parser("current")
    rev = sub.add_parser("revision")
    rev.add_argument("-m", "--message", required=True)
    rev.add_argument("--autogenerate", action="store_true")
    args = parser.parse_args(argv)

    cfg = make_config()
    if args.cmd == "upgrade":
        command.upgrade(cfg, args.revision)
    elif args.cmd == "downgrade":
        command.downgrade(cfg, args.revision)
    elif args.cmd == "current":
        command.current(cfg, verbose=True)
    elif args.cmd == "revision":
        command.revision(cfg, message=args.message, autogenerate=args.autogenerate)


if __name__ == "__main__":
    main()
