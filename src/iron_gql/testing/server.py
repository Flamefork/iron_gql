import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import asynccontextmanager
from contextlib import contextmanager

import pydantic

try:
    import uvicorn
except ImportError as exc:
    _MISSING = (
        "iron_gql.testing.server needs uvicorn; "
        "install it with `pip install iron-gql[testing]`"
    )
    raise ImportError(_MISSING) from exc

from iron_gql.runtime import ASGIApp

_STARTUP_TIMEOUT = 10
_SHUTDOWN_TIMEOUT = 5
# Whatever uvicorn raises is kept -- by the serving thread, or by the serving
# task itself -- so a server that has neither started nor failed by the
# deadline broke an invariant of uvicorn's, not one this helper can wait out.
_STARTUP_INVARIANT = (
    f"uvicorn neither started nor failed within {_STARTUP_TIMEOUT} seconds"
)

# `socket.getsockname` is typed as Any; validate the pair we know it returns
# for an IPv4 socket instead of casting.
_SOCKET_ADDRESS = pydantic.TypeAdapter(tuple[str, int])


def _wait_for_start(server: uvicorn.Server, failure: list[BaseException]) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while not server.started:
        if failure:
            raise failure[0]
        if time.monotonic() > deadline:
            raise AssertionError(_STARTUP_INVARIANT)
        time.sleep(0.01)


async def _async_wait_for_start(
    server: uvicorn.Server, serving: asyncio.Task[None]
) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while not server.started:
        if serving.done():
            serving.result()
        if time.monotonic() > deadline:
            raise AssertionError(_STARTUP_INVARIANT)
        await asyncio.sleep(0.01)


def _config(app: ASGIApp) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        log_level="warning",
        # Fakes do not speak the lifespan protocol, and an app served in the
        # test's own loop has its lifespan run by the test.
        lifespan="off",
        # wsproto ships transitively with iron_gql (httpx2[ws]); "auto"
        # would silently serve without websockets if it found nothing.
        ws="wsproto",
        # An unclosed connection must not hold up teardown.
        timeout_graceful_shutdown=1,
    )


# Binding before uvicorn starts and handing over the bound socket keeps the
# port ours the whole time; probing for a free port and passing the number
# would leave a window for another process to take it.
def _loopback_listener() -> tuple[socket.socket, int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    _host, port = _SOCKET_ADDRESS.validate_python(listener.getsockname())
    return listener, port


# Serves an ASGI app on a loopback port and yields its GraphQL URL. The
# synchronous client has no in-process transport: the ASGI transport of httpx2
# is async-only and WSGI cannot carry websockets. Sync and websocket tests
# therefore drive a fake app over a real socket.
#
# `path` only shapes the yielded URL: a fake app usually answers on every path,
# so nothing here routes.
@contextmanager
def live_asgi_server(app: ASGIApp, path: str = "/graphql") -> Iterator[str]:
    listener, port = _loopback_listener()
    with listener:
        server = uvicorn.Server(_config(app))
        failure: list[BaseException] = []

        def run() -> None:
            try:
                server.run(sockets=[listener])
            except BaseException as exc:  # noqa: BLE001
                # Including SystemExit: uvicorn exits the process on some
                # startup failures. The exception is re-raised on the calling
                # thread by `_wait_for_start`, never swallowed.
                failure.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            _wait_for_start(server, failure)
            yield f"http://127.0.0.1:{port}{path}"
        finally:
            server.should_exit = True
            thread.join(timeout=_SHUTDOWN_TIMEOUT)


# The same server, run as a task of the calling event loop rather than in a
# thread of its own. An SSE subscription has to reach the app over a socket --
# the ASGI transport of httpx2 holds the response body until the app returns --
# and this keeps that app in the loop the test runs in: whatever the test
# opened for it (pools, a lifespan, an event the app awaits) is used from the
# loop it belongs to.
@asynccontextmanager
async def async_live_asgi_server(
    app: ASGIApp, path: str = "/graphql"
) -> AsyncIterator[str]:
    listener, port = _loopback_listener()
    with listener:
        server = uvicorn.Server(_config(app))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await _async_wait_for_start(server, serving)
            yield f"http://127.0.0.1:{port}{path}"
        finally:
            server.should_exit = True
            await serving
