import asyncio
import json

from rl_worker.health import HealthServer


async def _get(port: int, path: str) -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    return status, json.loads(body)


async def test_health_endpoints_reflect_readiness() -> None:
    server = HealthServer("127.0.0.1", 0)
    ready = {"db": False}
    server.register("db", lambda: ready["db"])
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    try:
        assert (await _get(port, "/healthz"))[0] == 200
        status, body = await _get(port, "/readyz")
        assert status == 503
        assert body["checks"] == {"db": False}
        ready["db"] = True
        assert (await _get(port, "/readyz"))[0] == 200
        assert (await _get(port, "/nope"))[0] == 404
    finally:
        await server.stop()
