"""``python -m rl_api`` — run the API with uvicorn."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "rl_api.app:create_app",
        factory=True,
        host=os.environ.get("RL_API_HOST", "0.0.0.0"),  # noqa: S104  # nosec B104 - container
        port=int(os.environ.get("RL_API_PORT", "8000")),
        log_config=None,  # our JSON logging owns the root logger
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("RL_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
