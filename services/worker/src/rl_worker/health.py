"""Minimal HTTP health endpoint for the worker (stdlib asyncio, no web framework).

``GET /healthz`` → 200 while the process loop is alive; ``GET /readyz`` → 200 when every
registered component reports ready, else 503. Anything else → 404.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

log = logging.getLogger("rl_worker.health")


class HealthServer:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._ready_checks: dict[str, Callable[[], bool]] = {}
        self._server: asyncio.Server | None = None

    def register(self, name: str, is_ready: Callable[[], bool]) -> None:
        self._ready_checks[name] = is_ready

    def readiness(self) -> tuple[bool, dict[str, bool]]:
        states = {name: bool(check()) for name, check in self._ready_checks.items()}
        return all(states.values()), states

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("health server listening", extra={"port": self.port})

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2)
            parts = request_line.decode("latin-1").split()
            path = parts[1] if len(parts) >= 2 else ""
            body: dict[str, object]
            if path == "/healthz":
                status, body = 200, {"status": "ok"}
            elif path == "/readyz":
                ok, states = self.readiness()
                status, body = (
                    (200 if ok else 503),
                    {"status": "ready" if ok else "not_ready", "checks": states},
                )
            else:
                status, body = 404, {"error": {"code": "not_found", "message": "not found"}}
            payload = json.dumps(body).encode()
            reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[status]
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
