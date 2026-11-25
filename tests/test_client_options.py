"""The options of a generated module's client: the dict the module imports, its
types, and the settings it passes on to `httpx2`.

A setting of `httpx2` is observed by the server: `auth` writes an
`Authorization` header, which every request of the client carries, a websocket
upgrade too.
"""

import asyncio
import gc
import importlib
import inspect
import re
import sys
import textwrap
import weakref
from collections import UserDict
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Coroutine
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from contextlib import AbstractContextManager
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import ModuleType
from typing import Literal
from typing import Protocol
from typing import Self
from typing import cast
from typing import final
from typing import get_type_hints

import httpx2
import pydantic
import pytest

from iron_gql import SubscriptionInterruptedError
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import AsyncGQLClientOptions
from iron_gql.runtime import GQLClient
from iron_gql.runtime import GQLClientOptions
from iron_gql.runtime import SubscriptionProtocol
from iron_gql.testing import accept_graphql_ws
from iron_gql.testing import use_client
from iron_gql.testing.server import live_asgi_server
from tests.conftest import ProjectBuilder
from tests.conftest import SSETestRequest
from tests.conftest import accept_graphql_sse
from tests.conftest import basedpyright_report
from tests.conftest import generated_package
from tests.conftest import generated_source

_PACKAGE_SCHEMA = "type Query { ping: String! }"

# One object holds the endpoint and the options: the module imports it once.
_SETTINGS = """
from typing import ClassVar

from iron_gql.runtime import {options}


class CONFIG:
    url: ClassVar[str] = "http://testserver/graphql/"
    gql: ClassVar[{options}] = {{
        "headers": {{"X-Tenant": "t-1"}},
        "query_timeout": 30,
        "subscription_idle_timeout": 2.5,
        "httpx_options": {{"trust_env": False}},
    }}
"""


def _package_queries(package: str) -> str:
    return f"""
    from tests.generated.{package}.gql.api import api_gql

    ping = api_gql("query Ping {{ ping }}")
    """


generated_package(
    "client_options",
    schema=_PACKAGE_SCHEMA,
    queries=_package_queries("client_options"),
    settings=_SETTINGS.format(options="AsyncGQLClientOptions"),
    base_url_name="CONFIG.url",
    client_options_name="CONFIG.gql",
)
generated_package(
    "client_options_sync",
    schema=_PACKAGE_SCHEMA,
    queries=_package_queries("client_options_sync"),
    mode="sync",
    settings=_SETTINGS.format(options="GQLClientOptions"),
    base_url_name="CONFIG.url",
    client_options_name="CONFIG.gql",
)

type _Mode = Literal["sync", "async"]
type _Path = Literal["query", "sse", "ws"]

_MODES: tuple[_Mode, ...] = ("sync", "async")
_PATHS: tuple[_Path, ...] = ("query", "sse", "ws")

_AUTH = ("user", "pw")
_AUTH_HEADER = "Basic dXNlcjpwdw=="

# The limits under test, and the time the server outwaits them: a client that
# reads no limit gets its answer, and one under a limit gives up long before.
_IDLE = 0.2
_QUERY_TIMEOUT = 1
_PATIENCE = 4

_HEADER_PAIRS = pydantic.TypeAdapter(list[tuple[bytes, bytes]])


# Answers `ping` and counts to 1 in either protocol, over a websocket after more
# than `_IDLE` seconds for `late`. `slow` is answered, and `silent` -- a stream
# that is open -- counts, only to a client that still waits after `_PATIENCE`
# seconds. `/moved` redirects to `/graphql`. Records the headers of every
# request it answers, a websocket upgrade included.
@dataclass
class _Server:
    seen: list[tuple[_Path, dict[str, str]]] = field(default_factory=list)

    async def __call__(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        if scope["type"] == "websocket":
            pairs = _HEADER_PAIRS.validate_python(scope["headers"])
            headers = {key.decode(): value.decode() for key, value in pairs}
            self.seen.append(("ws", headers))
            connection = await accept_graphql_ws(scope, receive, send)
            subscription = await connection.ack()
            if "late" in cast("str", subscription.payload["query"]):
                await asyncio.sleep(_IDLE * 2)
            await subscription.next({"counter": 1})
            await subscription.complete()
            await connection.drain()
            return
        request = await accept_graphql_sse(scope, receive, send)
        if scope["path"] == "/moved":
            await send({
                "type": "http.response.start",
                "status": 307,
                "headers": [(b"location", b"/graphql")],
            })
            await send({"type": "http.response.body", "body": b""})
            return
        query = cast("str", request.body["query"])
        if "subscription" in query:
            await request.open_stream()
            # The server sends the head of a response with its first bytes.
            await request.write(b": open\n\n")
            if "silent" in query and await _hung_up(request):
                return
            self.seen.append(("sse", request.headers))
            await request.next({"data": {"counter": 1}})
            await request.complete()
            await request.end()
        else:
            if "slow" in query and await _hung_up(request):
                return
            self.seen.append(("query", request.headers))
            await request.respond(200, "application/json", b'{"data":{"ping":"pong"}}')

    def header(self, name: str) -> list[tuple[_Path, str | None]]:
        return [(path, headers.get(name)) for path, headers in self.seen]


async def _hung_up(request: SSETestRequest) -> bool:
    try:
        async with asyncio.timeout(_PATIENCE):
            await request.wait_disconnect()
    except TimeoutError:
        return False
    return True


@pytest.fixture
def server() -> Iterator[tuple[_Server, str]]:
    app = _Server()
    with live_asgi_server(app) as base_url:
        yield app, base_url


class _Pong(pydantic.BaseModel):
    ping: str


class _Count(pydantic.BaseModel):
    counter: int


def _protocol(path: _Path) -> SubscriptionProtocol:
    return "graphql-transport-ws" if path == "ws" else "graphql-sse"


def _send(client: GQLClient, path: _Path) -> None:
    if path == "query":
        client.query(_Pong, "query { ping }", variables={}, headers={})
        return
    with client.subscribe(
        _Count, "subscription { counter }", variables={}, headers={}
    ) as stream:
        assert [item.counter for item in stream] == [1]


async def _async_send(client: AsyncGQLClient, path: _Path) -> None:
    if path == "query":
        await client.query(_Pong, "query { ping }", variables={}, headers={})
        return
    async with client.subscribe(
        _Count, "subscription { counter }", variables={}, headers={}
    ) as stream:
        assert [item.counter async for item in stream] == [1]


@pytest.mark.parametrize("path", _PATHS)
def test_httpx_option_reaches_every_request_of_the_sync_client(
    server: tuple[_Server, str], path: _Path
):
    app, base_url = server
    client = GQLClient(
        base_url=base_url,
        subscription_protocol=_protocol(path),
        httpx_options={"auth": _AUTH},
    )
    try:
        _send(client, path)
    finally:
        client.close()
    assert app.header("authorization") == [(path, _AUTH_HEADER)]


# `asyncio.run` in the test's own thread, with the loop factory given: a run
# without one makes its loop the thread's current one, and the loop it replaces
# -- the one pytest-asyncio leaves after an async test, never closed -- is
# collected with a `ResourceWarning` inside whichever test comes next.
def _run[T](coroutine: Coroutine[object, object, T]) -> T:
    return asyncio.run(coroutine, loop_factory=asyncio.new_event_loop)


# Two runs are two event loops, each with a transport of its own.
@pytest.mark.parametrize("path", _PATHS)
def test_httpx_option_reaches_every_request_of_the_async_client_in_each_loop(
    server: tuple[_Server, str], path: _Path
):
    app, base_url = server
    client = AsyncGQLClient(
        base_url=base_url,
        subscription_protocol=_protocol(path),
        httpx_options={"auth": _AUTH},
    )
    _run(_async_send(client, path))
    _run(_async_send(client, path))
    assert app.header("authorization") == [(path, _AUTH_HEADER)] * 2


@pytest.mark.parametrize("path", ["query", "ws"])
async def test_httpx_option_reaches_an_app_called_in_process(path: _Path):
    app = _Server()
    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        subscription_protocol=_protocol(path),
        target_app=app,
        httpx_options={"auth": _AUTH},
    )
    try:
        await _async_send(client, path)
    finally:
        await client.close()
    assert app.header("authorization") == [(path, _AUTH_HEADER)]


@pytest.mark.parametrize("options", [None, {}], ids=["none", "empty"])
@pytest.mark.parametrize("mode", _MODES)
async def test_no_httpx_option_leaves_the_request_as_it_was(
    server: tuple[_Server, str], mode: _Mode, options: dict[str, object] | None
):
    app, base_url = server
    await _query(mode, base_url, options)
    assert app.header("authorization") == [("query", None)]


async def _query(
    mode: _Mode, base_url: str, httpx_options: Mapping[str, object] | None
) -> None:
    match mode:
        case "sync":
            client = GQLClient(
                base_url=base_url,
                subscription_protocol="graphql-sse",
                httpx_options=httpx_options,
            )
            try:
                _send(client, "query")
            finally:
                client.close()
        case "async":
            async_client = AsyncGQLClient(
                base_url=base_url,
                subscription_protocol="graphql-sse",
                httpx_options=httpx_options,
            )
            try:
                await _async_send(async_client, "query")
            finally:
                await async_client.close()


class _FalsyOptions(UserDict[str, object]):
    def __bool__(self) -> bool:
        return False


# Only `None` stands for no options: what a mapping is worth as a condition does
# not matter.
@pytest.mark.parametrize("mode", _MODES)
async def test_falsy_mapping_of_httpx_options_is_read(
    server: tuple[_Server, str], mode: _Mode
):
    app, base_url = server
    await _query(mode, base_url, _FalsyOptions(auth=_AUTH))
    assert app.header("authorization") == [("query", _AUTH_HEADER)]


async def test_key_added_after_creation_does_not_reach_the_client(
    server: tuple[_Server, str],
):
    app, base_url = server
    # Not empty: an empty dict would be replaced rather than copied.
    options: dict[str, object] = {"trust_env": False}
    client = GQLClient(
        base_url=base_url,
        subscription_protocol="graphql-sse",
        httpx_options=options,
    )
    async_client = AsyncGQLClient(
        base_url=base_url,
        subscription_protocol="graphql-sse",
        httpx_options=options,
    )
    options["auth"] = _AUTH
    try:
        _send(client, "query")
        await _async_send(async_client, "query")
    finally:
        client.close()
        await async_client.close()
    assert app.header("authorization") == [("query", None)] * 2


# What each client is created with beside `httpx_options`: the checks of the
# options do not depend on it.
_CLIENTS: dict[str, Callable[..., GQLClient | AsyncGQLClient]] = {
    "sync": GQLClient,
    "async": AsyncGQLClient,
}


def _rejection(
    mode: _Mode, error: type[Exception], httpx_options: dict[str, object]
) -> str:
    with pytest.raises(error) as exc:
        _CLIENTS[mode](
            base_url="http://testserver/graphql",
            subscription_protocol="graphql-sse",
            httpx_options=httpx_options,
        )
    assert type(exc.value) is error
    return str(exc.value)


_IN_PROCESS = "to call an app in process, pass target_app to AsyncGQLClient"
_OWNED: dict[str, str] = {
    "base_url": "'base_url' (pass base_url to the client)",
    "headers": "'headers' (pass headers to the client)",
    "timeout": (
        "'timeout' (pass query_timeout and subscription_idle_timeout to the client)"
    ),
    "cookies": "'cookies' (drop it, the client keeps its cookies itself)",
    "transport": f"'transport' ({_IN_PROCESS})",
    "mounts": f"'mounts' ({_IN_PROCESS})",
}


def _owned_message(*keys: str) -> str:
    listed = ", ".join(_OWNED[key] for key in keys)
    return f"httpx_options holds keys the client owns: {listed}"


def _unknown_message(mode: _Mode, *keys: str) -> str:
    http_type = {"sync": "Client", "async": "AsyncClient"}[mode]
    listed = ", ".join(repr(key) for key in keys)
    return (
        f"httpx_options holds keys that httpx2.{http_type} of httpx2 "
        f"{httpx2.__version__} does not take: {listed}; "
        "correct the spelling or drop them"
    )


# `None` is what `httpx2` itself takes for "not set": the key is refused whatever
# its value.
@pytest.mark.parametrize("value", [None, "x"], ids=["none", "value"])
@pytest.mark.parametrize("key", list(_OWNED))
@pytest.mark.parametrize("mode", _MODES)
def test_key_the_client_owns_is_refused(mode: _Mode, key: str, value: str | None):
    message = _rejection(mode, ValueError, {key: value})
    assert message == _owned_message(key)


@pytest.mark.parametrize("mode", _MODES)
def test_every_key_the_client_owns_is_named_at_once(mode: _Mode):
    owned = dict.fromkeys(reversed(_OWNED))
    message = _rejection(mode, ValueError, {"auth": _AUTH, **owned})
    assert message == _owned_message(*_OWNED)


@pytest.mark.parametrize("mode", _MODES)
def test_key_httpx2_does_not_take_is_refused_at_creation(mode: _Mode):
    message = _rejection(mode, TypeError, {"verfy": False, "auth": _AUTH, "self": 1})
    assert message == _unknown_message(mode, "verfy", "self")


@pytest.mark.parametrize("mode", _MODES)
def test_owned_key_is_refused_before_an_unknown_one(mode: _Mode):
    message = _rejection(mode, ValueError, {"verfy": False, "headers": {}})
    assert message == _owned_message("headers")


_PROXY_AND_APP = (
    "httpx_options sets proxy on a client with target_app: httpx2 would send the "
    "requests to the proxy instead of the app. Drop one of the two"
)


@pytest.mark.parametrize(
    ("httpx_options", "error", "message"),
    [
        ({}, ValueError, _PROXY_AND_APP),
        ({"headers": {}}, ValueError, _owned_message("headers")),
        ({"verfy": False}, TypeError, _unknown_message("async", "verfy")),
    ],
    ids=["alone", "with-an-owned-key", "with-an-unknown-key"],
)
def test_proxy_with_target_app_is_refused_after_the_keys(
    httpx_options: dict[str, object], error: type[Exception], message: str
):
    with pytest.raises(error) as exc:
        AsyncGQLClient(
            base_url="http://testserver/graphql",
            subscription_protocol="graphql-sse",
            target_app=_Server(),
            httpx_options={"proxy": "http://127.0.0.1:9", **httpx_options},
        )
    assert type(exc.value) is error
    assert str(exc.value) == message


# The names the README gives as examples.
@pytest.mark.parametrize("mode", _MODES)
def test_settings_the_readme_names_are_taken(mode: _Mode):
    _CLIENTS[mode](
        base_url="http://testserver/graphql",
        subscription_protocol="graphql-sse",
        httpx_options={
            "verify": True,
            "cert": None,
            "proxy": None,
            "limits": httpx2.Limits(),
            "auth": _AUTH,
            "event_hooks": {},
        },
    )


async def test_no_proxy_with_target_app_calls_the_app():
    app = _Server()
    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        subscription_protocol="graphql-sse",
        target_app=app,
        httpx_options={"proxy": None},
    )
    try:
        await _async_send(client, "query")
    finally:
        await client.close()
    assert [path for path, _headers in app.seen] == ["query"]


# A key `httpx2` takes: its value is `httpx2`'s to judge, as the client opens.
_FOREIGN_PROXY = {"proxy": "gopher://127.0.0.1"}


def test_value_httpx2_refuses_fails_the_sync_client_at_creation():
    with pytest.raises(ValueError, match="gopher"):
        GQLClient(
            base_url="http://testserver/graphql",
            subscription_protocol="graphql-sse",
            httpx_options=_FOREIGN_PROXY,
        )


async def test_value_httpx2_refuses_fails_the_async_client_at_its_first_operation():
    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        subscription_protocol="graphql-sse",
        httpx_options=_FOREIGN_PROXY,
    )
    with pytest.raises(ValueError, match="gopher"):
        await _async_send(client, "query")


# The options the generator has parameters of its own for.
_GENERATOR_OPTIONS = {"base_url", "subscription_protocol", "model_base"}


@pytest.mark.parametrize(
    ("options", "client"),
    [(GQLClientOptions, GQLClient), (AsyncGQLClientOptions, AsyncGQLClient)],
    ids=_MODES,
)
def test_options_type_holds_the_parameters_of_its_client(
    options: type[object], client: type[object]
):
    parameters = cast("dict[str, object]", get_type_hints(client.__init__))
    assert parameters.keys() >= _GENERATOR_OPTIONS
    assert cast("dict[str, object]", get_type_hints(options)) == {
        name: hint
        for name, hint in parameters.items()
        if name not in _GENERATOR_OPTIONS
    }
    assert inspect.signature(client).parameters.keys() == parameters.keys()


def test_every_option_may_be_left_out():
    assert GQLClientOptions.__required_keys__ == frozenset()
    assert AsyncGQLClientOptions.__required_keys__ == frozenset()


_SCHEMA = """
type Query {
    ping: String!
    slow: String!
}

type Subscription {
    counter: Int!
    silent: Int!
    late: Int!
}
"""

_QUERIES = """
from sample_app.gql.api import api_gql

ping = api_gql("query Ping { ping }")
slow = api_gql("query Slow { ping: slow }")
counter = api_gql("subscription Counter { counter }")
silent = api_gql("subscription Silent { counter: silent }")
late = api_gql("subscription Late { counter: late }")
"""

type _Headers = dict[str, str]


class _SyncOperation(Protocol):
    def with_headers(self, headers: _Headers) -> Self: ...

    def execute(self) -> object: ...


class _AsyncOperation(Protocol):
    def with_headers(self, headers: _Headers) -> Self: ...

    def execute(self) -> Coroutine[object, object, object]: ...


class _SyncSubscription(Protocol):
    def execute(self) -> AbstractContextManager[Iterator[_Count]]: ...


class _AsyncSubscription(Protocol):
    def execute(self) -> AbstractAsyncContextManager[AsyncIterator[_Count]]: ...


# A generated module of either mode, its operations called by name. An async
# one runs each in an event loop of its own.
@dataclass
class _Module:
    mode: _Mode
    api: ModuleType
    queries: dict[str, object]

    def query(self, name: str = "ping", headers: _Headers | None = None) -> object:
        if self.mode == "sync":
            operation = cast("_SyncOperation", self.queries[name])
            return operation.with_headers(headers or {}).execute()
        async_operation = cast("_AsyncOperation", self.queries[name])
        return _run(async_operation.with_headers(headers or {}).execute())

    def counters(self, name: str = "counter") -> list[int]:
        if self.mode == "sync":
            with cast("_SyncSubscription", self.queries[name]).execute() as stream:
                return [item.counter for item in stream]

        async def collect() -> list[int]:
            subscription = cast("_AsyncSubscription", self.queries[name])
            async with subscription.execute() as stream:
                return [item.counter async for item in stream]

        return _run(collect())


# The module the generator writes for `options`, a Python expression that
# `sample_app.settings` holds as `OPTIONS`.
@contextmanager
def _module(
    test_project: ProjectBuilder,
    mode: _Mode,
    base_url: str,
    options: str,
    protocol: SubscriptionProtocol = "graphql-sse",
    model_base_import: str = "pydantic:BaseModel",
) -> Iterator[_Module]:
    test_project.prepare(schema=_SCHEMA, queries=_QUERIES)
    test_project.write_file(
        test_project.root / "sample_app/settings.py",
        f"GRAPHQL_URL = {base_url!r}\nOPTIONS = {options}\n",
    )
    test_project.generate(
        mode=mode,
        subscription_protocol=protocol,
        client_options_import="sample_app.settings:OPTIONS",
        model_base_import=model_base_import,
    )
    api = test_project.import_api()
    queries = importlib.import_module("sample_app.queries")
    try:
        yield _Module(mode, api, vars(queries))
    finally:
        client = cast("GQLClient | AsyncGQLClient", vars(api)["_client"])
        if isinstance(client, AsyncGQLClient):
            _run(client.close())
        else:
            client.close()


@pytest.mark.parametrize("mode", _MODES)
def test_module_options_hold_for_every_operation(
    test_project: ProjectBuilder, server: tuple[_Server, str], mode: _Mode
):
    app, base_url = server
    options = (
        f'{{"headers": {{"x-tenant": "t-1"}}, "httpx_options": {{"auth": {_AUTH}}}}}'
    )
    with _module(test_project, mode, base_url, options) as module:
        module.query()
        assert module.counters() == [1]
    assert app.header("x-tenant") == [("query", "t-1"), ("sse", "t-1")]
    assert app.header("authorization") == [
        ("query", _AUTH_HEADER),
        ("sse", _AUTH_HEADER),
    ]


@pytest.mark.parametrize("mode", _MODES)
def test_module_options_hold_for_websocket_subscriptions(
    test_project: ProjectBuilder, server: tuple[_Server, str], mode: _Mode
):
    app, base_url = server
    options = (
        f'{{"headers": {{"x-tenant": "t-1"}}, "httpx_options": {{"auth": {_AUTH}}}}}'
    )
    with _module(
        test_project, mode, base_url, options, "graphql-transport-ws"
    ) as module:
        assert module.counters() == [1]
    assert app.header("x-tenant") == [("ws", "t-1")]
    assert app.header("authorization") == [("ws", _AUTH_HEADER)]


@pytest.mark.parametrize("mode", _MODES)
def test_module_query_timeout_ends_a_slow_query(
    test_project: ProjectBuilder, server: tuple[_Server, str], mode: _Mode
):
    _app, base_url = server
    options = f'{{"query_timeout": {_QUERY_TIMEOUT}}}'
    with (
        _module(test_project, mode, base_url, options) as module,
        pytest.raises(httpx2.ReadTimeout),
    ):
        module.query("slow")


@pytest.mark.parametrize("mode", _MODES)
def test_module_idle_timeout_ends_a_silent_sse_subscription(
    test_project: ProjectBuilder, server: tuple[_Server, str], mode: _Mode
):
    _app, base_url = server
    options = f'{{"subscription_idle_timeout": {_IDLE}}}'
    with (
        _module(test_project, mode, base_url, options) as module,
        pytest.raises(SubscriptionInterruptedError),
    ):
        module.counters("silent")


@pytest.mark.parametrize("mode", _MODES)
def test_websocket_subscription_does_not_read_the_idle_timeout(
    test_project: ProjectBuilder, server: tuple[_Server, str], mode: _Mode
):
    _app, base_url = server
    options = f'{{"subscription_idle_timeout": {_IDLE}}}'
    with _module(
        test_project, mode, base_url, options, "graphql-transport-ws"
    ) as module:
        assert module.counters("late") == [1]


def test_async_module_calls_the_app_its_options_name(test_project: ProjectBuilder):
    # `settings` is written by the test, so the app it names is put there too.
    app = _Server()
    with _module(
        test_project,
        "async",
        "http://testserver/graphql",
        '{"target_app": lambda *event: APP[0](*event)}\nAPP = []',
        "graphql-transport-ws",
    ) as module:
        settings = importlib.import_module("sample_app.settings")
        cast("list[ASGIApp]", vars(settings)["APP"]).append(app)
        module.query()
        assert module.counters() == [1]
    assert [path for path, _headers in app.seen] == ["query", "ws"]


@pytest.mark.parametrize("mode", _MODES)
def test_follow_redirects_ends_the_refusal_of_a_redirect(
    test_project: ProjectBuilder, server: tuple[_Server, str], mode: _Mode
):
    _app, base_url = server
    moved = base_url.replace("/graphql", "/moved")
    with (
        _module(test_project, mode, moved, "{}") as module,
        pytest.raises(httpx2.HTTPStatusError),
    ):
        module.query()
    followed = '{"httpx_options": {"follow_redirects": True}}'
    with _module(test_project, mode, moved, followed) as module:
        assert module.query() is not None


@pytest.mark.parametrize("mode", _MODES)
def test_auth_overwrites_the_authorization_of_an_operation(
    test_project: ProjectBuilder, server: tuple[_Server, str], mode: _Mode
):
    app, base_url = server
    own = {"Authorization": "Bearer token"}
    with _module(test_project, mode, base_url, "{}") as module:
        module.query(headers=own)
    with _module(
        test_project, mode, base_url, f'{{"httpx_options": {{"auth": {_AUTH}}}}}'
    ) as module:
        module.query(headers=own)
    assert app.header("authorization") == [
        ("query", "Bearer token"),
        ("query", _AUTH_HEADER),
    ]


def test_client_set_by_use_client_takes_no_module_options(
    test_project: ProjectBuilder, server: tuple[_Server, str]
):
    app, base_url = server
    options = '{"headers": {"x-tenant": "t-1"}}'
    with _module(test_project, "sync", base_url, options) as module:
        module.query()
        own = GQLClient(base_url=base_url, subscription_protocol="graphql-sse")
        with use_client(module.api, own):
            module.query()
    assert app.header("x-tenant") == [("query", "t-1"), ("query", None)]


# Python's own refusal of a keyword, raised where the module calls its client.
# The module is on a base that is not the default, which it passes itself.
@pytest.mark.parametrize(
    ("mode", "options", "message"),
    [
        ("sync", '{"tymeout": 1}', "unexpected keyword argument 'tymeout'"),
        ("async", '{"tymeout": 1}', "unexpected keyword argument 'tymeout'"),
        ("sync", '{"target_app": None}', "unexpected keyword argument 'target_app'"),
        (
            "sync",
            '{"base_url": "x"}',
            "multiple values for keyword argument 'base_url'",
        ),
        (
            "sync",
            '{"model_base": None}',
            "multiple values for keyword argument 'model_base'",
        ),
        (
            "async",
            '{"subscription_protocol": "graphql-sse"}',
            "multiple values for keyword argument 'subscription_protocol'",
        ),
    ],
)
def test_option_the_client_does_not_take_fails_the_import_of_the_module(
    test_project: ProjectBuilder, mode: _Mode, options: str, message: str
):
    with (
        pytest.raises(TypeError, match=message),
        _module(
            test_project,
            mode,
            "http://testserver/graphql",
            options,
            model_base_import="pydantic.main:BaseModel",
        ),
    ):
        pytest.fail("the module imported")


# One of every kind of name a module binds, beside the models and result union
# aliases that are named by their shape.
_NAMES_SCHEMA = """
type Query {
    post(filter: PostFilter): Post!
    item: Item!
}

type Post {
    id: ID!
    state: State!
    author: User!
    attachment: Attachment!
}

type User {
    id: ID!
    name: String!
}

type Item {
    id: ID!
    owner: User!
    media: Attachment!
}

enum State {
    DRAFT
}

input PostFilter {
    id: ID
}

union Attachment = Image | Link

type Image {
    url: String!
}

type Link {
    href: String!
}
"""

_SHAPES_QUERIES = """
from sample_app.gql.api import api_gql

get_post = api_gql('''
    query GetPost($filter: PostFilter) {
        post(filter: $filter) { id state author { id name } }
    }
''')
get_item = api_gql('''
    query GetItem {
        item {
            media {
                __typename
                ... on Image { url }
                ... on Link { href }
            }
        }
    }
''')
"""

_NAMES_QUERIES = f"""{_SHAPES_QUERIES}
get_slotted = api_gql('''
    query GetSlotted {{ item {{ owner {{ id }} media @slot {{ __typename }} }} }}
''')
image_url = api_gql("fragment ImageUrl on Image {{ url }}")
get_slotted.bind(media=image_url)
"""

_PINNED_NAMES = (
    # The scaffold: its imports, its classes, the builtins it reads, its values.
    "runtime",
    "pydantic",
    "GQLModel",
    "TResult",
    "list",
    "api_gql",
    "_client",
    # What another option imports.
    "builtins",
    # An operation, its result root, a template, its bound base and a model of
    # its slot's subtree, a fragment with its on-type base and its model.
    "GetPostQuery",
    "GetPostQueryResult",
    "GetSlottedQuery",
    "GetSlottedQueryBound",
    "GetSlottedQueryResult",
    "ImageUrlFragment",
    "OnImage",
    "ImageUrlData",
    # An enum and the model of an input type keep the type's name.
    "State",
    "PostFilter",
)


# The base URL's import is the oracle: whatever it is told about a name, the
# import of the client options is told the same.
@pytest.mark.parametrize("name", _PINNED_NAMES)
def test_client_options_import_is_refused_a_name_as_the_base_url_import_is(
    test_project: ProjectBuilder, name: str
):
    test_project.prepare(schema=_NAMES_SCHEMA, queries=_NAMES_QUERIES)
    target = f"sample_app.other:{name}"
    with pytest.raises(GraphQLGenerationError) as of_base_url:
        test_project.render(base_url_import=target)
    with pytest.raises(GraphQLGenerationError) as of_options:
        test_project.render(client_options_import=target)
    assert of_options.value.errors == of_base_url.value.errors
    assert f"from sample_app.other import {name}" in str(of_options.value)


def test_client_options_import_is_refused_the_name_of_the_base_url_import(
    test_project: ProjectBuilder,
):
    test_project.prepare(schema=_NAMES_SCHEMA, queries=_NAMES_QUERIES)
    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render(
            base_url_import="sample_app.settings:CONFIG.url",
            client_options_import="sample_app.other:CONFIG.gql",
        )
    assert exc.value.errors == [
        (
            "Name 'CONFIG' is claimed by from sample_app.settings import CONFIG and by "
            "from sample_app.other import CONFIG; rename the operation, template, "
            "fragment or GraphQL type, or change the generator option, behind one of "
            "them"
        )
    ]


# The root of a dotted name is what the import binds.
@pytest.mark.parametrize("suffix", ["", ".gql"], ids=["name", "dotted"])
@pytest.mark.parametrize("name", ["User", "Attachment"], ids=["model", "union-alias"])
def test_shape_takes_its_next_name_beside_the_client_options_import(
    test_project: ProjectBuilder, name: str, suffix: str
):
    test_project.prepare(schema=_NAMES_SCHEMA, queries=_SHAPES_QUERIES)
    plain = set(vars(test_project.generate_and_import()[0]))
    assert name in plain

    settings = test_project.root / "sample_app/settings.py"
    held = "{}" if suffix == "" else "type('Config', (), {'gql': {}})"
    test_project.write_file(
        settings, f"{settings.read_text(encoding='utf-8')}{name} = {held}\n"
    )
    api, _queries = test_project.generate_and_import(
        client_options_import=f"sample_app.settings:{name}{suffix}"
    )
    assert vars(api)[name] is vars(sys.modules["sample_app.settings"])[name]
    [moved] = set(vars(api)) - plain
    assert re.fullmatch(
        {"User": "UserWithIdName", "Attachment": "Attachment_[0-9a-f]{8}"}[name], moved
    )


@pytest.mark.parametrize(
    ("package", "client"),
    [("client_options", "AsyncGQLClient"), ("client_options_sync", "GQLClient")],
)
def test_options_beside_the_base_url_are_read_from_one_import(
    package: str, client: str
):
    source = generated_source(package)
    settings = f"from tests.generated.{package}.settings import CONFIG\n"
    assert source.count(settings) == 1
    assert (
        f"""\
_client = runtime.{client}(
    base_url=CONFIG.url,
    subscription_protocol="graphql-sse",
    **CONFIG.gql,
)
"""
        in source
    )


# `final.__annotations__` is an empty dict behind a name the scaffold imports.
def test_options_behind_a_name_of_the_scaffold_are_read_from_its_import(
    test_project: ProjectBuilder,
):
    test_project.prepare(schema=_SCHEMA, queries=_QUERIES)
    api, _queries = test_project.generate_and_import(
        client_options_import="typing:final.__annotations__"
    )
    assert vars(api)["final"] is final


class _HookedClient(GQLClient):
    def __init__(self) -> None:
        super().__init__(
            base_url="http://testserver/graphql",
            subscription_protocol="graphql-sse",
            httpx_options={"event_hooks": {"request": [self.on_request]}},
        )

    def on_request(self, _request: httpx2.Request) -> None:
        raise NotImplementedError


def test_client_its_own_options_refer_to_is_collected_once_closed():
    ref = weakref.ref(_HookedClient())
    gc.collect()
    client = ref()
    assert client is not None
    client.close()
    del client
    gc.collect()
    assert ref() is None


# The README's own example of a typed dict, as the settings of a project.
def _readme_settings() -> str:
    readme = Path(__file__).parent.parent / "README.md"
    blocks = [
        match.group(1)
        for match in re.finditer(
            r"```python\n(.*?)```", readme.read_text(encoding="utf-8"), re.DOTALL
        )
    ]
    [block] = [block for block in blocks if "AsyncGQLClientOptions" in block]
    return textwrap.dedent(block)


def test_readme_options_make_a_module_that_imports(test_project: ProjectBuilder):
    test_project.prepare(schema=_SCHEMA, queries=_QUERIES)
    config = test_project.root / "sample_app/config.py"
    test_project.write_file(config, _readme_settings())
    test_project.generate(
        base_url_import="sample_app.config:GRAPHQL_URL",
        client_options_import="sample_app.config:CLIENT_OPTIONS",
    )
    assert isinstance(vars(test_project.import_api())["_client"], AsyncGQLClient)


# `PYTHONPATH` is how basedpyright, checking under the configuration of this
# repository, finds the project's own modules: it asks the interpreter.
def test_basedpyright_judges_the_options_dict_and_its_module(
    test_project: ProjectBuilder, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PYTHONPATH", str(test_project.root))
    test_project.prepare(
        schema=_SCHEMA,
        queries="""
        from sample_app.gql.async_api import async_api_gql
        from sample_app.gql.sync_api import sync_api_gql

        ping = async_api_gql("query Ping { ping }")
        slow = async_api_gql("query Slow { slow }")
        sync_ping = sync_api_gql("query Ping { ping }")
        sync_slow = sync_api_gql("query Slow { slow }")
        """,
    )
    package = test_project.root / "sample_app"
    test_project.write_file(package / "py.typed", "")
    test_project.write_file(package / "config.py", _readme_settings())
    test_project.write_file(
        package / "misspelled.py",
        """
        from iron_gql.runtime import GQLClientOptions

        OPTIONS: GQLClientOptions = {"query_timeot": 30}
        """,
    )
    modes: tuple[_Mode, ...] = ("async", "sync")
    for mode in modes:
        test_project.generate(
            mode=mode,
            module_full_name=f"sample_app.gql.{mode}_api",
            base_url_import="sample_app.config:GRAPHQL_URL",
            client_options_import="sample_app.config:CLIENT_OPTIONS",
        )

    report = basedpyright_report(package)

    found = {
        (item.file.name, item.rule, item.message.splitlines()[0])
        for item in report.general_diagnostics
        if item.severity == "error"
    }
    # The async module and the README's dict are clean.
    assert found == {
        (
            "misspelled.py",
            "reportAssignmentType",
            (
                'Type "dict[str, int]" is not assignable to declared type '
                '"GQLClientOptions"'
            ),
        ),
        (
            "sync_api.py",
            "reportCallIssue",
            "Unable to match unpacked TypedDict argument to parameters",
        ),
    }
