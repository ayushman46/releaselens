"""``python -m rl_pipeline <task> [--params-json '{...}' | --params-file in.json]``

Logs go to stderr (JSON lines by default, ``RL_LOG_FORMAT=console`` for humans); the task's
result dict is printed as one JSON object on the **last stdout line**. Exit code 1 on failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from rl_core.obs import JsonFormatter

log = logging.getLogger("rl_pipeline")


def configure_cli_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("RL_LOG_FORMAT", "json") == "console":
        handler.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    else:
        handler.setFormatter(JsonFormatter("rl-pipeline"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("RL_LOG_LEVEL", "INFO").upper())
    for noisy in ("botocore", "boto3", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _parse_params(args: argparse.Namespace) -> dict[str, Any]:
    raw = Path(args.params_file).read_text(encoding="utf-8") if args.params_file else None
    raw = raw if raw is not None else args.params_json
    params = json.loads(raw) if raw else {}
    if not isinstance(params, dict):
        raise SystemExit("--params-json/--params-file must contain a JSON object")
    return params


def main(argv: list[str] | None = None) -> int:
    from rl_pipeline.tasks import TASKS

    parser = argparse.ArgumentParser(prog="python -m rl_pipeline", description=__doc__)
    parser.add_argument("task", choices=sorted(TASKS))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--params-json", default="{}", help="JSON object passed to the task")
    group.add_argument("--params-file", help="file containing the JSON object")
    args = parser.parse_args(argv)

    configure_cli_logging()
    params = _parse_params(args)
    try:
        result = TASKS[args.task](params)
    except Exception:
        log.exception("task failed", extra={"task": args.task})
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
