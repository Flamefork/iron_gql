import asyncio
import gc
import importlib
import subprocess
import sys
import threading
import warnings
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Coroutine
from collections.abc import Iterator
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from contextlib import AbstractContextManager
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Literal
from typing import Protocol
from typing import cast

import httpx2
import pydantic
import pytest

from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.runtime import SubscriptionProtocol
from iron_gql.testing import accept_graphql_ws
from iron_gql.testing.server import live_asgi_server
from tests.conftest import ProjectBuilder
from tests.conftest import accept_graphql_sse

_SCHEMA = """
type Query { ping: String! }
type Subscription { counter: Int! }
"""

_CLOSED = "The client is closed"

_QUERIES = """
from sample_app.gql.api import api_gql

ping = api_gql("query Ping { ping }")
counter = api_gql("subscription Counter { counter }")
"""


# The operations of `_QUERIES`, typed: the module is generated per test, so the
# test names what it calls once rather than at every call.
class _Pong(Protocol):
    ping: str


class _Count(Protocol):
    counter: int


class _SyncPing(Protocol):
    def execute(self) -> _Pong: ...


class _SyncCounter(Protocol):
    def execute(self) -> AbstractContextManager[Iterator[_Count]]: ...


class _AsyncPing(Protocol):
    def execute(self) -> Awaitable[_Pong]: ...


class _AsyncCounter(Protocol):
    def execute(self) -> AbstractAsyncContextManager[AsyncIterator[_Count]]: ...


# A server that keeps its connections alive, as a production one does: a
# connection the client's pool holds on to is what a client has to close. Every
# query response sets a cookie; every request records the cookie it came with.
@dataclass
class _Server:
    cookies: list[tuple[str, str | None]] = field(default_factory=list)
    query_ports: list[int] = field(default_factory=list)

    async def __call__(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        if scope["type"] == "websocket":
            connection = await accept_graphql_ws(scope, receive, send)
            self.cookies.append(("ws", _cookie(connection.scope)))
            subscription = await connection.ack()
            await subscription.next({"counter": 1})
            await subscription.complete()
            await connection.drain()
            return
        request = await accept_graphql_sse(scope, receive, send)
        if request.headers["accept"] == "text/event-stream":
            self.cookies.append(("sse", request.headers.get("cookie")))
            await request.open_stream()
            await request.next({"data": {"counter": 1}})
            await request.complete()
            await request.end()
            return
        self.cookies.append(("query", request.headers.get("cookie")))
        _host, port = _PEER.validate_python(scope["client"])
        self.query_ports.append(port)
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"set-cookie", b"session=abc; Path=/"),
            ],
        })
        await send({"type": "http.response.body", "body": b'{"data":{"ping":"pong"}}'})


_HEADER_PAIRS = pydantic.TypeAdapter(list[tuple[bytes, bytes]])
_PEER = pydantic.TypeAdapter(tuple[str, int])


def _cookie(scope: ASGIScope) -> str | None:
    cookie = dict(_HEADER_PAIRS.validate_python(scope["headers"])).get(b"cookie")
    return None if cookie is None else cookie.decode()


@pytest.fixture
def server() -> Iterator[tuple[_Server, str]]:
    app = _Server()
    with live_asgi_server(app) as base_url:
        yield app, base_url


# A fresh interpreter, so what it prints at exit is its own: under pytest the
# test process outlives the client and `filterwarnings` decides what a warning
# becomes.
def _run_child(root: str, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# The open file descriptors of the child, counted by the OS rather than asked of
# the client: a closed connection is one descriptor fewer.
_OPEN_FDS = """
import os

def open_fds():
    return len(os.listdir("/dev/fd"))
"""


def test_sync_module_client_closes_its_connection_at_exit(
    test_project: ProjectBuilder, server: tuple[_Server, str]
):
    _app, base_url = server
    test_project.prepare(schema=_SCHEMA, queries=_QUERIES, base_url=base_url)
    test_project.generate(mode="sync")

    # Exit hooks run last registered first: this one, registered before the
    # module exists, runs after every hook the module's client registers.
    child = _run_child(
        str(test_project.root),
        _OPEN_FDS
        + """
import atexit

atexit.register(lambda: print("closed at exit:", open_fds() == connected - 1))

from sample_app import queries

assert queries.ping.execute().ping == "pong"
connected = open_fds()
""",
    )

    assert (child.returncode, child.stdout, child.stderr) == (
        0,
        "closed at exit: True\n",
        "",
    )


def test_sync_client_dropped_without_close_closes_its_connection_on_collection(
    server: tuple[_Server, str], tmp_path: Path
):
    _app, base_url = server

    child = _run_child(
        str(tmp_path),
        _OPEN_FDS
        + f"""
import gc

import pydantic

from iron_gql.runtime import GQLClient


class Ping(pydantic.BaseModel):
    ping: str


client = GQLClient(base_url={base_url!r}, subscription_protocol="graphql-sse")
client.query(Ping, "query {{ ping }}", variables={{}}, headers={{}})
connected = open_fds()
del client
gc.collect()
print("closed on collection:", open_fds() == connected - 1)
""",
    )

    assert (child.returncode, child.stdout, child.stderr) == (
        0,
        "closed on collection: True\n",
        "",
    )


def test_sync_client_connection_is_inherited_by_a_forked_child(
    server: tuple[_Server, str], tmp_path: Path
):
    app, base_url = server

    child = _run_child(
        str(tmp_path),
        f"""
import os
import warnings

import pydantic

from iron_gql.runtime import GQLClient


class Ping(pydantic.BaseModel):
    ping: str


def ping():
    client.query(Ping, "query {{ ping }}", variables={{}}, headers={{}})


client = GQLClient(base_url={base_url!r}, subscription_protocol="graphql-sse")
ping()
# macOS counts the threads its own network stack started for the request; the
# child below makes one request and exits, so none of them is needed there.
warnings.filterwarnings("ignore", ".* is multi-threaded", DeprecationWarning)
pid = os.fork()
if pid == 0:
    ping()
    os._exit(0)
os.waitpid(pid, 0)
""",
    )

    assert (child.returncode, child.stderr) == (0, "")
    [parent_port, child_port] = app.query_ports
    assert child_port == parent_port


@pytest.mark.parametrize("protocol", ["graphql-sse", "graphql-transport-ws"])
def test_sync_client_refuses_operations_after_close(
    test_project: ProjectBuilder,
    server: tuple[_Server, str],
    protocol: SubscriptionProtocol,
):
    _app, base_url = server
    test_project.prepare(schema=_SCHEMA, queries=_QUERIES, base_url=base_url)
    test_project.generate(mode="sync", subscription_protocol=protocol)
    client = cast("GQLClient", vars(test_project.import_api())["_client"])
    queries = vars(importlib.import_module(f"{test_project.package}.queries"))
    ping = cast("_SyncPing", queries["ping"])
    counter = cast("_SyncCounter", queries["counter"])
    assert ping.execute().ping == "pong"

    client.close()
    client.close()

    with pytest.raises(RuntimeError, match=_CLOSED):
        ping.execute()
    with pytest.raises(RuntimeError, match=_CLOSED), counter.execute():
        pass


class _LoopTransport(Protocol):
    http: httpx2.AsyncClient


type _Transports = dict[asyncio.AbstractEventLoop, _LoopTransport]


# The client's open transports by loop. Read off the client because what is
# asserted is exactly that bookkeeping: an entry per live loop, none left for a
# loop that has ended.
def _transports(client: AsyncGQLClient) -> _Transports:
    return cast("_Transports", vars(client)["_transports"])


# A generated async module whose client was built at import, outside any loop.
@dataclass
class _AsyncModule:
    client: AsyncGQLClient
    ping: _AsyncPing
    counter: _AsyncCounter

    async def pong(self) -> str:
        return (await self.ping.execute()).ping

    async def counters(self) -> list[int]:
        async with self.counter.execute() as stream:
            return [item.counter async for item in stream]


def _async_module(
    test_project: ProjectBuilder,
    base_url: str,
    protocol: SubscriptionProtocol = "graphql-sse",
) -> _AsyncModule:
    test_project.prepare(schema=_SCHEMA, queries=_QUERIES, base_url=base_url)
    api, queries_module = test_project.generate_and_import(protocol)
    queries = vars(queries_module)
    return _AsyncModule(
        client=cast("AsyncGQLClient", vars(api)["_client"]),
        ping=cast("_AsyncPing", queries["ping"]),
        counter=cast("_AsyncCounter", queries["counter"]),
    )


# `asyncio.run` in the test's own thread, with the loop factory given: a run
# without one makes its loop the thread's current one, and the loop it replaces
# -- the one pytest-asyncio leaves after an async test, never closed -- is
# collected with a `ResourceWarning` inside whichever test comes next.
def _run[T](coroutine: Coroutine[object, object, T]) -> T:
    return asyncio.run(coroutine, loop_factory=asyncio.new_event_loop)


def test_async_module_client_serves_consecutive_asyncio_runs(
    test_project: ProjectBuilder, server: tuple[_Server, str]
):
    _app, base_url = server
    module = _async_module(test_project, base_url)

    assert _run(module.pong()) == "pong"
    assert _run(module.pong()) == "pong"

    assert _transports(module.client) == {}
    gc.collect()


def test_async_client_serves_loops_of_several_threads_at_once(
    test_project: ProjectBuilder, server: tuple[_Server, str]
):
    _app, base_url = server
    module = _async_module(test_project, base_url)
    threads = 4
    barrier = threading.Barrier(threads)

    async def requests() -> list[str]:
        await asyncio.to_thread(barrier.wait)
        return list(await asyncio.gather(*(module.pong() for _ in range(3))))

    def run_loop(_thread: int) -> list[str]:
        return asyncio.run(requests())

    with ThreadPoolExecutor(threads) as pool:
        results = list(pool.map(run_loop, range(threads)))

    assert results == [["pong"] * 3] * threads
    assert _transports(module.client) == {}
    gc.collect()


def test_async_client_drops_loops_closed_without_shutting_down_their_generators(
    test_project: ProjectBuilder, server: tuple[_Server, str]
):
    _app, base_url = server
    module = _async_module(test_project, base_url)
    results: list[tuple[str, int]] = []

    # A loop closed by hand runs no `shutdown_asyncgens`: its transport is left
    # to the collector, which warns about the connection it still holds.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        for _ in range(3):
            loop = asyncio.new_event_loop()
            pong = loop.run_until_complete(module.pong())
            results.append((pong, len(_transports(module.client))))
            loop.close()
        _run(module.client.close())
        gc.collect()

    assert results == [("pong", 1)] * 3
    assert _transports(module.client) == {}
    assert {warning.category for warning in caught} == {ResourceWarning}


@pytest.mark.parametrize("protocol", ["graphql-sse", "graphql-transport-ws"])
def test_async_client_carries_cookies_across_loops_into_subscriptions(
    test_project: ProjectBuilder,
    server: tuple[_Server, str],
    protocol: SubscriptionProtocol,
):
    app, base_url = server
    module = _async_module(test_project, base_url, protocol)

    async def follow_up() -> tuple[str, list[int]]:
        return await module.pong(), await module.counters()

    _run(module.pong())
    assert _run(follow_up()) == ("pong", [1])

    subscription = "sse" if protocol == "graphql-sse" else "ws"
    assert app.cookies == [
        ("query", None),
        ("query", "session=abc"),
        (subscription, "session=abc"),
    ]
    gc.collect()


# A loop in a thread of its own that runs the coroutines it is given until it
# is stopped. An `asyncio.Runner` ends it, so its async generators are shut
# down the way a loop ends in production.
class _LoopThread:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        started: Future[asyncio.AbstractEventLoop] = Future()
        self._thread = threading.Thread(target=self._serve, args=(started,))
        self._thread.start()
        self.loop = started.result(10)

    # The runner is made in the thread it runs in: a runner sets its loop as
    # the current one of the thread that makes it.
    def _serve(self, started: Future[asyncio.AbstractEventLoop]) -> None:
        with asyncio.Runner() as runner:
            started.set_result(runner.get_loop())
            runner.run(self._stop.wait())

    def run[T](self, coroutine: Coroutine[object, object, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(10)

    def stop(self) -> None:
        if self._thread.is_alive():
            self.loop.call_soon_threadsafe(self._stop.set)
            self._thread.join()


@contextmanager
def _loop_thread() -> Iterator[_LoopThread]:
    thread = _LoopThread()
    try:
        yield thread
    finally:
        thread.stop()


@pytest.mark.parametrize("protocol", ["graphql-sse", "graphql-transport-ws"])
@pytest.mark.parametrize("other_loop_ends_by", ["shutdown", "close"])
def test_async_close_is_final_in_every_loop_and_closes_each_transport_in_its_own(
    test_project: ProjectBuilder,
    server: tuple[_Server, str],
    protocol: SubscriptionProtocol,
    other_loop_ends_by: Literal["shutdown", "close"],
):
    _app, base_url = server
    module = _async_module(test_project, base_url, protocol)

    with _loop_thread() as closing, _loop_thread() as other:
        closing.run(module.pong())
        other.run(module.pong())
        transports = _transports(module.client)
        closing_http = transports[closing.loop].http
        other_http = transports[other.loop].http

        closing.run(module.client.close())

        assert closing_http.is_closed
        assert closing.loop not in transports
        for loop in (closing, other):
            with pytest.raises(RuntimeError, match=_CLOSED):
                loop.run(module.pong())
            with pytest.raises(RuntimeError, match=_CLOSED):
                loop.run(module.counters())
        assert not other_http.is_closed

        match other_loop_ends_by:
            case "shutdown":
                other.stop()
            case "close":
                other.run(module.client.close())
        assert other_http.is_closed
        assert transports == {}
    gc.collect()


def test_async_close_called_concurrently_in_one_loop_closes_the_transport_once(
    test_project: ProjectBuilder, server: tuple[_Server, str]
):
    _app, base_url = server
    module = _async_module(test_project, base_url)

    async def close_twice() -> httpx2.AsyncClient:
        await module.pong()
        http = _transports(module.client)[asyncio.get_running_loop()].http
        await asyncio.gather(module.client.close(), module.client.close())
        return http

    assert _run(close_twice()).is_closed
    assert _transports(module.client) == {}


def test_async_client_keeps_the_transports_of_loops_stopped_but_not_closed(
    test_project: ProjectBuilder, server: tuple[_Server, str]
):
    _app, base_url = server
    module = _async_module(test_project, base_url)
    loops = [asyncio.new_event_loop() for _ in range(3)]
    counts: list[int] = []

    for loop in loops:
        loop.run_until_complete(module.pong())
        counts.append(len(_transports(module.client)))
    for loop in loops:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    assert counts == [1, 2, 3]
    assert _transports(module.client) == {}


async def _ok_app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
    await _Server()(scope, receive, send)


# `target_app` serves no graphql-sse subscription, and says so with a
# `ValueError`; a closed client says first that it is closed.
@pytest.mark.parametrize("protocol", ["graphql-sse", "graphql-transport-ws"])
async def test_async_client_with_target_app_refuses_operations_after_close(
    protocol: SubscriptionProtocol,
):
    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        subscription_protocol=protocol,
        target_app=_ok_app,
    )
    await client.close()

    with pytest.raises(RuntimeError, match=_CLOSED):
        await client.query(_PingModel, "query { ping }", variables={}, headers={})
    with pytest.raises(RuntimeError, match=_CLOSED):
        async with client.subscribe(
            _CounterModel, "subscription { counter }", variables={}, headers={}
        ):
            pass


class _PingModel(pydantic.BaseModel):
    ping: str


class _CounterModel(pydantic.BaseModel):
    counter: int


async def test_async_client_sends_the_headers_it_was_created_with():
    seen: list[str | None] = []

    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        request = await accept_graphql_sse(scope, receive, send)
        seen.append(request.headers.get("x-tenant"))
        await request.respond(200, "application/json", b'{"data":{"ping":"pong"}}')

    headers = {"X-Tenant": "first"}
    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        subscription_protocol="graphql-sse",
        target_app=app,
        headers=headers,
    )
    headers["X-Tenant"] = "second"
    try:
        await client.query(_PingModel, "query { ping }", variables={}, headers={})
    finally:
        await client.close()

    assert seen == ["first"]


def test_async_operation_started_by_another_generator_cleanup_outlives_the_loop(
    test_project: ProjectBuilder, server: tuple[_Server, str]
):
    _app, base_url = server
    module = _async_module(test_project, base_url)
    results: list[str] = []

    # Waits in its cleanup until the client's own hook has closed the loop's
    # transport, then queries: the shutdown already took its generators.
    async def cleanup_that_queries() -> AsyncIterator[None]:
        try:
            yield
        finally:
            await asyncio.sleep(0.1)
            results.append(await module.pong())

    async def main() -> AsyncIterator[None]:
        await module.pong()
        generator = cleanup_that_queries()
        await anext(generator)
        return generator

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        generator = _run(main())
        left = len(_transports(module.client))
        _run(module.client.close())
        del generator
        gc.collect()

    assert (results, left) == (["pong"], 1)
    assert _transports(module.client) == {}
    assert {warning.category for warning in caught} == {ResourceWarning}


def test_async_close_completes_under_an_eager_task_factory():
    def eager_loop() -> asyncio.AbstractEventLoop:
        loop = asyncio.new_event_loop()
        loop.set_task_factory(asyncio.eager_task_factory)
        return loop

    async def query_and_close() -> httpx2.AsyncClient:
        client = AsyncGQLClient(
            base_url="http://testserver/graphql",
            subscription_protocol="graphql-sse",
            target_app=_ok_app,
        )
        await client.query(_PingModel, "query { ping }", variables={}, headers={})
        http = _transports(client)[asyncio.get_running_loop()].http
        await asyncio.wait_for(client.close(), 5)
        return http

    assert asyncio.run(query_and_close(), loop_factory=eager_loop).is_closed


async def test_async_close_cancelled_in_one_caller_completes_for_the_others():
    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        subscription_protocol="graphql-sse",
        target_app=_ok_app,
    )
    await client.query(_PingModel, "query { ping }", variables={}, headers={})
    http = _transports(client)[asyncio.get_running_loop()].http

    cancelled = asyncio.create_task(client.close())
    waiting = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    cancelled.cancel()
    await waiting

    assert cancelled.cancelled()
    assert http.is_closed
    assert _transports(client) == {}
