import asyncio
import importlib
import json
import re
import socket
import sys
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import MutableMapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextlib import suppress
from types import ModuleType
from typing import Literal
from typing import cast
from typing import override
from urllib.parse import urlsplit

import httpx2
import pydantic
import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from iron_gql.runtime import ASGIApp
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.testing import use_client
from iron_gql.testing.server import async_live_asgi_server
from iron_gql.testing.server import live_asgi_server
from tests.conftest import ProjectBuilder
from tests.conftest import build_schema
from tests.conftest import generated_package
from tests.conftest import setup_httpserver

_PING_SCHEMA = """
type Query {
    ping: String!
}
"""

generated_package(
    "testing_async_swap",
    schema=_PING_SCHEMA,
    queries='''
    from tests.generated.testing_async_swap.gql.api import api_gql

    ping = api_gql(
        """
        query Ping {
            ping
        }
        """
    )
    ''',
)

generated_package(
    "testing_sync_swap",
    mode="sync",
    schema=_PING_SCHEMA,
    queries='''
    from tests.generated.testing_sync_swap.gql.api import api_gql

    ping = api_gql(
        """
        query Ping {
            ping
        }
        """
    )
    ''',
)

from tests.generated.testing_async_swap import queries as async_swap_queries
from tests.generated.testing_sync_swap import queries as sync_swap_queries

type _Event = MutableMapping[str, object]
type _Receive = Callable[[], Awaitable[_Event]]
type _Send = Callable[[_Event], Awaitable[None]]


class _PingResult(pydantic.BaseModel):
    ping: str


def _resolve_ping(_root: None, _info: GraphQLResolveInfo[object]) -> str:
    return "pong"


def _ping_url(httpserver: HTTPServer) -> str:
    return setup_httpserver(
        httpserver, build_schema(_PING_SCHEMA, {"Query": {"ping": _resolve_ping}})
    )


async def _ok_app(scope: _Event, receive: _Receive, send: _Send) -> None:
    assert scope["type"] == "http"
    await receive()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": b'{"data": {"ping": "pong"}}'})


def _broken_app(*_args: object) -> Awaitable[None]:
    # uvicorn probes a loaded app by calling it with no arguments to detect an
    # app factory; anything but a TypeError from that probe aborts startup.
    msg = "broken app"
    raise ValueError(msg)


def test_generated_client_binding_has_fixed_name(
    test_project: ProjectBuilder,
):
    test_project.gql_module = "sample_app.gql.reports"
    test_project.prepare(
        schema=_PING_SCHEMA,
        queries="""
        from sample_app.gql.reports import reports_gql

        ping = reports_gql('query Ping { ping }')
        """,
    )
    api_module, _ = test_project.generate_and_import()

    binding = cast("dict[str, object]", vars(api_module))["_client"]

    assert isinstance(binding, AsyncGQLClient)


async def test_use_client_swaps_restores_and_closes_async_client(
    httpserver: HTTPServer,
):
    api_module = importlib.import_module("tests.generated.testing_async_swap.gql.api")
    namespace = cast("dict[str, object]", vars(api_module))
    original = namespace["_client"]
    client = AsyncGQLClient(
        subscription_protocol="graphql-sse", base_url=_ping_url(httpserver)
    )

    # `ping` was constructed at module import, before the client is replaced.
    async with use_client(api_module, client) as active:
        assert active is client
        assert namespace["_client"] is client
        result = await async_swap_queries.ping.execute()
        assert result.ping == "pong"

    assert namespace["_client"] is original
    with pytest.raises(RuntimeError, match="closed"):
        await client.query(_PingResult, "query Ping { ping }", variables={}, headers={})


def test_use_client_swaps_restores_and_closes_sync_client(httpserver: HTTPServer):
    api_module = importlib.import_module("tests.generated.testing_sync_swap.gql.api")
    namespace = cast("dict[str, object]", vars(api_module))
    original = namespace["_client"]
    client = GQLClient(
        subscription_protocol="graphql-sse", base_url=_ping_url(httpserver)
    )

    # `ping` was constructed at module import, before the client is replaced.
    with use_client(api_module, client) as active:
        assert active is client
        assert namespace["_client"] is client
        result = sync_swap_queries.ping.execute()
        assert result.ping == "pong"

    assert namespace["_client"] is original
    with pytest.raises(RuntimeError, match="closed"):
        client.query(_PingResult, "query Ping { ping }", variables={}, headers={})


type _Mode = Literal["sync", "async"]
type _Client = GQLClient | AsyncGQLClient
type _Held = (
    Generator[None, BaseException | None] | AsyncGenerator[None, BaseException | None]
)


def _named_app(name: str) -> ASGIApp:
    # Answers `ping` with its own name, so a test sees which client a request
    # of the module went through.
    body = json.dumps({"data": {"ping": name}}).encode()

    async def app(scope: _Event, receive: _Receive, send: _Send) -> None:
        assert scope["type"] == "http"
        await receive()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})

    return app


# Carries what the module bound when the client began to close.
class _CloseFailedError(Exception):
    def __init__(self, module: ModuleType) -> None:
        super().__init__()
        self.bound = cast("dict[str, object]", vars(module))["_client"]


class _FailingCloseClient(GQLClient):
    def __init__(self, module: ModuleType, *, base_url: str) -> None:
        super().__init__(base_url=base_url, subscription_protocol="graphql-sse")
        self._module = module

    @override
    def close(self) -> None:
        bound_error = _CloseFailedError(self._module)
        super().close()
        raise bound_error


class _FailingCloseAsyncClient(AsyncGQLClient):
    def __init__(self, module: ModuleType, *, target_app: ASGIApp) -> None:
        super().__init__(
            base_url="http://testserver/graphql",
            subscription_protocol="graphql-sse",
            target_app=target_app,
        )
        self._module = module

    @override
    async def close(self) -> None:
        bound_error = _CloseFailedError(self._module)
        await super().close()
        raise bound_error


# A `use_client` block held open at its body: the test sends the body on to
# its end, so swaps can exit in any order.
def _hold_sync(
    module: ModuleType, client: GQLClient
) -> Generator[None, BaseException | None]:
    with use_client(module, client):
        exc = yield
        if exc is not None:
            raise exc


async def _hold_async(
    module: ModuleType, client: AsyncGQLClient
) -> AsyncGenerator[None, BaseException | None]:
    async with use_client(module, client):
        exc = yield
        if exc is not None:
            raise exc


# Drives `use_client` of the sync or the async ping module and tells which app
# a request reached.
class _Swaps:
    def __init__(self, mode: _Mode, servers: ExitStack) -> None:
        self._mode: _Mode = mode
        self._servers = servers
        self.module = importlib.import_module(
            f"tests.generated.testing_{mode}_swap.gql.api"
        )
        self.original = self.bound

    @property
    def bound(self) -> object:
        return cast("dict[str, object]", vars(self.module))["_client"]

    def client(
        self,
        name: str,
        *,
        failing_close: bool = False,
        model_base: type[pydantic.BaseModel] | None = None,
    ) -> _Client:
        app = _named_app(name)
        match self._mode:
            case "sync":
                base_url = self._servers.enter_context(live_asgi_server(app))
                if failing_close:
                    return _FailingCloseClient(self.module, base_url=base_url)
                return GQLClient(
                    base_url=base_url,
                    subscription_protocol="graphql-sse",
                    model_base=model_base,
                )
            case "async":
                if failing_close:
                    return _FailingCloseAsyncClient(self.module, target_app=app)
                return AsyncGQLClient(
                    base_url="http://testserver/graphql",
                    subscription_protocol="graphql-sse",
                    target_app=app,
                    model_base=model_base,
                )

    async def enter(self, client: _Client) -> _Held:
        match client:
            case GQLClient():
                held = _hold_sync(self.module, client)
                next(held)
                return held
            case AsyncGQLClient():
                held = _hold_async(self.module, client)
                await anext(held)
                return held

    # Lets the body of a held swap end, raising `exc` if given, and exits it.
    async def exit(self, held: _Held, exc: BaseException | None = None) -> None:
        match held:
            case Generator():
                with suppress(StopIteration):
                    held.send(exc)
            case AsyncGenerator():
                with suppress(StopAsyncIteration):
                    await held.asend(exc)

    async def ping(self) -> str:
        match self._mode:
            case "sync":
                return sync_swap_queries.ping.execute().ping
            case "async":
                return (await async_swap_queries.ping.execute()).ping

    async def assert_closed(self, client: _Client) -> None:
        match client:
            case GQLClient():
                with pytest.raises(RuntimeError, match="closed"):
                    client.query(
                        _PingResult, "query { ping }", variables={}, headers={}
                    )
            case AsyncGQLClient():
                with pytest.raises(RuntimeError, match="closed"):
                    await client.query(
                        _PingResult, "query { ping }", variables={}, headers={}
                    )

    # The module is back on its own client, and no swap is left on record: a
    # stale one would be bound again when a fresh swap exits. The module's own
    # client points at a host no test serves: its request fails over HTTP --
    # to connect, or with a proxy's error status -- rather than reach a swap's
    # app or hit a closed client.
    async def assert_released(self) -> None:
        assert self.bound is self.original
        with pytest.raises(httpx2.HTTPError):
            await self.ping()
        await self.exit(await self.enter(self.client("fresh")))
        assert self.bound is self.original


@pytest.fixture(params=["sync", "async"])
def swaps(request: pytest.FixtureRequest) -> Iterator[_Swaps]:
    with ExitStack() as servers:
        yield _Swaps(cast("_Mode", request.param), servers)


def _out_of_order(swaps: _Swaps) -> str:
    return rf"use_client of {re.escape(swaps.module.__name__)} exited while .* Nest"


async def test_nested_swaps_bind_the_inner_then_the_outer_then_the_original(
    swaps: _Swaps,
):
    outer = swaps.client("outer")
    inner = swaps.client("inner")

    outer_swap = await swaps.enter(outer)
    assert await swaps.ping() == "outer"
    inner_swap = await swaps.enter(inner)
    assert await swaps.ping() == "inner"
    await swaps.exit(inner_swap)
    assert await swaps.ping() == "outer"
    await swaps.exit(outer_swap)

    await swaps.assert_closed(inner)
    await swaps.assert_closed(outer)
    await swaps.assert_released()


async def test_overlapping_swaps_fail_the_one_that_exits_first(swaps: _Swaps):
    first = swaps.client("first")
    second = swaps.client("second")
    first_swap = await swaps.enter(first)
    second_swap = await swaps.enter(second)

    with pytest.raises(RuntimeError, match=_out_of_order(swaps)):
        await swaps.exit(first_swap)
    assert await swaps.ping() == "second"
    await swaps.exit(second_swap)

    await swaps.assert_closed(first)
    await swaps.assert_closed(second)
    await swaps.assert_released()


async def test_a_middle_swap_exiting_first_fails_alone(swaps: _Swaps):
    outer_swap = await swaps.enter(swaps.client("outer"))
    middle_swap = await swaps.enter(swaps.client("middle"))
    inner_swap = await swaps.enter(swaps.client("inner"))

    with pytest.raises(RuntimeError, match=_out_of_order(swaps)):
        await swaps.exit(middle_swap)
    assert await swaps.ping() == "inner"
    await swaps.exit(inner_swap)
    assert await swaps.ping() == "outer"
    await swaps.exit(outer_swap)

    await swaps.assert_released()


async def test_a_body_exception_passes_through_a_restored_swap(swaps: _Swaps):
    client = swaps.client("swap")
    swap = await swaps.enter(client)

    with pytest.raises(ValueError, match="body"):
        await swaps.exit(swap, ValueError("body"))

    await swaps.assert_closed(client)
    await swaps.assert_released()


async def test_a_cancelled_swap_restores_and_closes_its_client():
    module = importlib.import_module("tests.generated.testing_async_swap.gql.api")
    namespace = cast("dict[str, object]", vars(module))
    original = namespace["_client"]
    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        subscription_protocol="graphql-sse",
        target_app=_named_app("swap"),
    )
    entered = asyncio.Event()

    async def body() -> None:
        async with use_client(module, client):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(body())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert namespace["_client"] is original
    with pytest.raises(RuntimeError, match="closed"):
        await client.query(_PingResult, "query { ping }", variables={}, headers={})


async def test_an_out_of_order_exit_shadows_the_body_exception(swaps: _Swaps):
    first_swap = await swaps.enter(swaps.client("first"))
    second_swap = await swaps.enter(swaps.client("second"))

    with pytest.raises(RuntimeError, match=_out_of_order(swaps)) as raised:
        await swaps.exit(first_swap, ValueError("body"))
    assert isinstance(raised.value.__context__, ValueError)
    assert await swaps.ping() == "second"
    await swaps.exit(second_swap)

    await swaps.assert_released()


async def test_a_close_failure_shadows_an_out_of_order_exit(swaps: _Swaps):
    first_swap = await swaps.enter(swaps.client("first", failing_close=True))
    second = swaps.client("second")
    second_swap = await swaps.enter(second)

    with pytest.raises(_CloseFailedError) as raised:
        await swaps.exit(first_swap)
    assert raised.value.bound is second
    assert await swaps.ping() == "second"
    await swaps.exit(second_swap)

    await swaps.assert_released()


async def test_a_close_failure_leaves_the_module_restored(swaps: _Swaps):
    swap = await swaps.enter(swaps.client("swap", failing_close=True))

    with pytest.raises(_CloseFailedError) as raised:
        await swaps.exit(swap)

    assert raised.value.bound is swaps.original
    await swaps.assert_released()


class _ForeignModel(pydantic.BaseModel):
    pass


async def test_a_refused_inner_swap_leaves_the_outer_one_active(swaps: _Swaps):
    outer_swap = await swaps.enter(swaps.client("outer"))
    refused = swaps.client("refused", model_base=_ForeignModel)

    with pytest.raises(ValueError, match="pass the module's base or none"):
        await swaps.enter(refused)
    assert await swaps.ping() == "outer"
    await swaps.exit(outer_swap)

    await swaps.assert_released()


def test_a_sync_swap_holds_in_worker_threads():
    module = importlib.import_module("tests.generated.testing_sync_swap.gql.api")
    with live_asgi_server(_named_app("swap")) as base_url:
        client = GQLClient(base_url=base_url, subscription_protocol="graphql-sse")
        with use_client(module, client), ThreadPoolExecutor() as pool:
            answer = pool.submit(lambda: sync_swap_queries.ping.execute().ping)
            assert answer.result() == "swap"


def test_live_asgi_server_defaults_to_the_graphql_path():
    with live_asgi_server(_ok_app) as base_url:
        assert base_url.endswith("/graphql")


def test_live_asgi_server_serves_the_given_path_and_frees_the_port():
    # Only the request reaches the app: the server runs no lifespan.
    scope_types: list[object] = []

    async def app(scope: _Event, receive: _Receive, send: _Send) -> None:
        scope_types.append(scope["type"])
        await _ok_app(scope, receive, send)

    with live_asgi_server(app, path="/custom") as base_url:
        assert base_url.endswith("/custom")
        response = httpx2.post(base_url, json={"query": "query Ping { ping }"})
        assert response.json() == {"data": {"ping": "pong"}}
    assert scope_types == ["http"]

    port = urlsplit(base_url).port
    assert port is not None
    with socket.socket() as probe:
        probe.settimeout(2)
        with pytest.raises(ConnectionRefusedError):
            probe.connect(("127.0.0.1", port))


def test_live_asgi_server_reraises_a_failed_startup():
    with pytest.raises(ValueError, match="broken app"), live_asgi_server(_broken_app):
        pass


async def test_async_live_asgi_server_serves_in_the_calling_loop():
    # The app runs as a task of this loop, so it can await what the test owns.
    # Only the request reaches the app: the server runs no lifespan.
    seen_from_app: list[tuple[object, asyncio.AbstractEventLoop]] = []

    async def app(scope: _Event, receive: _Receive, send: _Send) -> None:
        seen_from_app.append((scope["type"], asyncio.get_running_loop()))
        await _ok_app(scope, receive, send)

    async with async_live_asgi_server(app, path="/custom") as base_url:
        assert base_url.endswith("/custom")
        async with httpx2.AsyncClient() as client:
            response = await client.post(base_url, json={"query": "query { ping }"})
        assert response.json() == {"data": {"ping": "pong"}}

    assert seen_from_app == [("http", asyncio.get_running_loop())]
    port = urlsplit(base_url).port
    assert port is not None
    with socket.socket() as probe:
        probe.settimeout(2)
        with pytest.raises(ConnectionRefusedError):
            probe.connect(("127.0.0.1", port))


async def test_async_live_asgi_server_reraises_a_failed_startup():
    with pytest.raises(ValueError, match="broken app"):
        async with async_live_asgi_server(_broken_app):
            pass


def test_server_import_without_the_extra_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    monkeypatch.delitem(sys.modules, "iron_gql.testing.server")

    with pytest.raises(ImportError, match=r"iron-gql\[testing\]"):
        importlib.import_module("iron_gql.testing.server")


def test_client_helpers_import_without_the_extra(monkeypatch: pytest.MonkeyPatch):
    # The client swap needs nothing beyond the runtime, so the extra must not
    # gate it: a consumer that only replaces clients installs plain iron-gql.
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    monkeypatch.delitem(sys.modules, "iron_gql.testing")
    monkeypatch.delitem(sys.modules, "iron_gql.testing.client")

    module = importlib.import_module("iron_gql.testing")

    assert "use_client" in dir(module)
