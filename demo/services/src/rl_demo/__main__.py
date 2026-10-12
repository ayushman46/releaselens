"""``python -m rl_demo`` — run the demo service selected by ``RL_DEMO_SERVICE``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "rl_demo.app:create_app",
        factory=True,
        host=os.environ.get("RL_DEMO_HOST", "0.0.0.0"),  # noqa: S104  # nosec B104 - container
        port=int(os.environ.get("RL_DEMO_PORT", "8000")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
