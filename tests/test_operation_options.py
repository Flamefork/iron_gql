"""The options of an operation (ADR 0010): what `with_options` takes, how its
options merge with each other and with the client's, and that they reach the
request of every kind of operation, through generated modules of both modes.

Which wait each limit ends, and what it raises, is `tests/test_error_model.py`'s.
"""

import asyncio
from collections import UserDict
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import MutableMapping
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Literal
from typing import cast
from typing import get_type_hints
from typing import override

import httpx2
import pydantic
import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Response

from iron_gql import HTTPResponseError
from iron_gql import TransportError
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.runtime import GQLClientOptions
from iron_gql.runtime import GQLOperation
from iron_gql.runtime import GQLOperationOptions
from iron_gql.testing import accept_graphql_ws
from iron_gql.testing.server import live_asgi_server
from tests.conftest import ProjectBuilder
from tests.conftest import accept_graphql_sse
from tests.conftest import basedpyright_report
from tests.conftest import decode_headers
from tests.conftest import generated_package
from tests.conftest import readme_block
from tests.conftest import run_async
from tests.conftest import use_package_client
from tests.conftest import use_sync_package_client

_SCHEMA = """
type Query {
    ping: String!
    node: Node!
    user(id: ID!): User
}

type User {
    id: ID!
}

type Mutation {
    touch: String!
}

type Subscription {
    counter: Int!
}

type Node {
    id: ID!
    thumbnail(width: Int!): String!
}
"""


def _package_queries(package: str) -> str:
    return f"""
    from tests.generated.{package}.gql.api import api_gql

    patient = api_gql("query Patient {{ ping }}").with_timeout(60)
    ping = api_gql("query Ping {{ ping }}")
    get_user = api_gql("query GetUser($id: ID!) {{ user(id: $id) {{ id }} }}")
    touch = api_gql("mutation Touch {{ touch }}")
    counter = api_gql("subscription Counter {{ counter }}")
    get_node = api_gql("query GetNode {{ node @slot {{ __typename }} }}")
    thumbnail = api_gql("fragment Thumbnail on Node {{ thumbnail(width: $width) }}")
    """


generated_package(
    "operation_options",
    schema=_SCHEMA,
    queries=_package_queries("operation_options"),
)
generated_package(
    "operation_options_sync",
    schema=_SCHEMA,
    queries=_package_queries("operation_options_sync"),
    mode="sync",
)

from tests.generated.operation_options import queries
from tests.generated.operation_options.gql import api
from tests.generated.operation_options_sync import queries as sync_queries

type _Mode = Literal["sync", "async"]
type _Path = Literal["query", "mutation", "binding", "sse", "ws", "declared"]

_MODES: tuple[_Mode, ...] = ("sync", "async")
_PATHS: tuple[_Path, ...] = ("query", "mutation", "binding", "sse", "ws")

_every_mode = pytest.mark.parametrize("mode", _MODES)
_every_path = pytest.mark.parametrize("path", _PATHS)

_AUTH = ("user", "pw")
_AUTH_HEADER = "Basic dXNlcjpwdw=="

# A limit that ends the wait for an answer that takes `_DELAY`, and one that
# does not.
_SHORT = 0.1
_DELAY = 0.4
_LONG = 10


# A header that comes more than once holds every value it came with.
def _joined_headers(scope: ASGIScope) -> dict[str, str]:
    joined: dict[str, str] = {}
    for name, value in decode_headers(scope):
        joined[name] = f"{joined[name]}, {value}" if name in joined else value
    return joined


_ANSWERS = {
    "ping": b'{"ping": "pong"}',
    "touch": b'{"touch": "pong"}',
    "user": b'{"user": {"id": "1"}}',
    "node": b'{"node": {"__typename": "Node", "thumbnail": "small"}}',
}


# Answers every operation of the modules after `delay` seconds, a WebSocket with
# its `connection_ack`. Records the headers and the variables of what it answers.
@dataclass
class _Server:
    delay: float = 0
    seen: list[dict[str, str]] = field(default_factory=list)
    variables: list[object] = field(default_factory=list)

    async def __call__(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:
            await self._http(scope, receive, send)

    async def _websocket(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        self.seen.append(_joined_headers(scope))
        connection = await accept_graphql_ws(scope, receive, send)
        await asyncio.sleep(self.delay)
        subscription = await connection.ack()
        await subscription.next({"counter": 1})
        await subscription.complete()
        await connection.drain()

    async def _http(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        request = await accept_graphql_sse(scope, receive, send)
        self.seen.append(_joined_headers(scope))
        self.variables.append(request.body.get("variables"))
        await asyncio.sleep(self.delay)
        query = request.body["query"]
        if "subscription" in str(query):
            await request.open_stream()
            await request.next({"data": {"counter": 1}})
            await request.complete()
            await request.end()
            return
        [data] = [data for field, data in _ANSWERS.items() if field in str(query)]
        await request.respond(200, "application/json", b'{"data": %b}' % data)

    def header(self, name: str) -> list[str | None]:
        return [headers.get(name) for headers in self.seen]


# One server for all the tests that take it, and what it saw in the test alone.
@pytest.fixture(scope="module")
def module_server() -> Iterator[tuple[_Server, str]]:
    app = _Server()
    with live_asgi_server(app) as base_url:
        yield app, base_url


@pytest.fixture
def server(module_server: tuple[_Server, str]) -> tuple[_Server, str]:
    app, _base_url = module_server
    app.seen.clear()
    app.variables.clear()
    return module_server


# What a test makes of an operation before it is executed.
class _Change:
    def __init__(self, *layers: GQLOperationOptions):
        self._layers = layers

    def __call__[T: GQLOperation](self, operation: T) -> T:
        for layer in self._layers:
            operation = operation.with_options(**layer)
        return operation


# The same by the short methods, each layer an option they take.
class _ShortChange(_Change):
    @override
    def __call__[T: GQLOperation](self, operation: T) -> T:
        for layer in self._layers:
            match layer:
                case {"timeout": timeout}:
                    operation = operation.with_timeout(timeout)
                case {"headers": headers}:
                    operation = operation.with_headers(headers)
                case _:
                    pytest.fail(f"no short method for {layer}")
        return operation


# Executes the operation of `path`, as changed, and judges its result.
def _sync_execute(path: _Path, change: _Change) -> None:
    match path:
        case "query":
            assert change(sync_queries.ping).execute().ping == "pong"
        case "declared":
            assert change(sync_queries.patient).execute().ping == "pong"
        case "mutation":
            assert change(sync_queries.touch).execute().touch == "pong"
        case "binding":
            image = sync_queries.thumbnail.with_args(width=7)
            bound = change(sync_queries.get_node.bind(node=image))
            node = image.read(bound.execute().node)
            assert node is not None
            assert node.thumbnail == "small"
        case "sse" | "ws":
            with change(sync_queries.counter).execute() as stream:
                assert [item.counter for item in stream] == [1]


async def _async_execute(path: _Path, change: _Change) -> None:
    match path:
        case "query":
            assert (await change(queries.ping).execute()).ping == "pong"
        case "declared":
            assert (await change(queries.patient).execute()).ping == "pong"
        case "mutation":
            assert (await change(queries.touch).execute()).touch == "pong"
        case "binding":
            image = queries.thumbnail.with_args(width=7)
            bound = change(queries.get_node.bind(node=image))
            node = image.read((await bound.execute()).node)
            assert node is not None
            assert node.thumbnail == "small"
        case "sse" | "ws":
            async with change(queries.counter).execute() as stream:
                assert [item.counter async for item in stream] == [1]


# Executes the operation of `path` under each of `changes` in turn, all on one
# client set by `use_client`.
def _execute(
    mode: _Mode,
    path: _Path,
    url: str,
    client_options: GQLClientOptions,
    *changes: _Change,
) -> None:
    _execute_all(mode, url, client_options, [(path, change) for change in changes])


def _execute_all(
    mode: _Mode,
    url: str,
    client_options: GQLClientOptions,
    steps: list[tuple[_Path, _Change]],
) -> None:
    protocol = "graphql-transport-ws" if steps[0][0] == "ws" else "graphql-sse"
    if mode == "sync":
        with use_sync_package_client(
            "operation_options_sync", url, protocol, **client_options
        ):
            for path, change in steps:
                _sync_execute(path, change)
        return

    async def run() -> None:
        async with use_package_client(
            "operation_options", url, protocol, **client_options
        ):
            for path, change in steps:
                await _async_execute(path, change)

    run_async(run())


# Records the extensions of every request a client sends, as they stand when the
# request goes out.
class _Extensions:
    def __init__(self, mode: _Mode):
        self._mode = mode
        self.seen: list[dict[str, object]] = []

    def _record(self, request: httpx2.Request) -> None:
        timeout = cast("Mapping[str, object]", request.extensions["timeout"])
        self.seen.append({**request.extensions, "timeout": dict(timeout)})

    async def _async_record(self, request: httpx2.Request) -> None:
        await asyncio.sleep(0)
        self._record(request)

    def reads(self) -> list[object]:
        return [
            cast("dict[str, object]", seen["timeout"])["read"] for seen in self.seen
        ]

    def hook(self) -> dict[str, Any]:
        hook = self._record if self._mode == "sync" else self._async_record
        return {"event_hooks": {"request": [hook]}}


# --- the types ---------------------------------------------------------------


def test_options_of_an_operation_are_those_of_its_client():
    hints: dict[str, object] = get_type_hints(GQLOperationOptions)
    assert hints == get_type_hints(GQLClientOptions)
    assert GQLOperationOptions.__required_keys__ == frozenset()


# `PYTHONPATH` is how basedpyright finds the project's own modules.
def test_basedpyright_keeps_the_type_of_an_operation_and_judges_the_names(
    test_project: ProjectBuilder, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PYTHONPATH", str(test_project.root))
    test_project.prepare(
        schema=_SCHEMA,
        queries="""
        from iron_gql.runtime import GQLOperationOptions
        from sample_app.gql.api import api_gql

        HEAVY: GQLOperationOptions = {"timeout": 60, "connect_timeout": None}

        ping = api_gql("query Ping { ping }")
        chained = api_gql("query Chained { ping }").with_options(timeout=60)
        get_node = api_gql("query GetNode { node @slot { __typename } }")
        node_id = api_gql("fragment NodeID on Node { id }")
        bound = get_node.bind(node=node_id)
        """,
    )
    package = test_project.root / "sample_app"
    test_project.write_file(package / "py.typed", "")
    test_project.generate()
    test_project.write_file(
        package / "typed.py",
        """
        from sample_app import queries

        reveal_type(queries.ping)
        reveal_type(queries.ping.with_options(timeout=60))
        reveal_type(queries.ping.with_options(**queries.HEAVY))
        reveal_type(queries.ping.with_timeout(None).with_headers({"X-A": "1"}))
        reveal_type(queries.bound)
        reveal_type(queries.bound.with_options(**queries.HEAVY))
        reveal_type(queries.chained)
        queries.ping.with_options(timeuot=60)
        queries.ping.with_options(target_app=None)
        """,
    )

    report = basedpyright_report(package / "typed.py", package / "queries.py")

    revealed = [
        item.message.partition(" is ")[2]
        for item in report.general_diagnostics
        if item.severity == "information"
    ]
    assert revealed[0] == '"PingQuery"'
    assert revealed[1:4] == [revealed[0]] * 3
    assert revealed[4].startswith('"GetNodeQueryBound[')
    assert revealed[5] == revealed[4]
    assert revealed[6:] == ['"ChainedQuery"']
    assert [
        (item.rule, item.message)
        for item in report.general_diagnostics
        if item.severity == "error"
    ] == [
        ("reportCallIssue", 'No parameter named "timeuot"'),
        ("reportCallIssue", 'No parameter named "target_app"'),
    ]


# --- what an operation holds -----------------------------------------------------


def test_with_options_returns_a_copy():
    plain = GQLOperation()
    changed = plain.with_options(timeout=1, stream_idle_timeout=None)
    assert plain.options == {}
    assert changed.options == {"timeout": 1, "stream_idle_timeout": None}


def test_short_methods_are_with_options_with_one_option():
    plain = GQLOperation().with_options(connect_timeout=2)
    timed = plain.with_timeout(1)
    assert timed.options == plain.with_options(timeout=1).options
    assert plain.with_timeout(None).options == {"connect_timeout": 2, "timeout": None}
    headed = plain.with_headers({"X-A": "1"})
    assert headed.options == plain.with_options(headers={"X-A": "1"}).options
    assert headed.options.get("headers") == {"X-A": "1"}


def test_an_option_that_a_call_does_not_name_is_kept():
    operation = GQLOperation().with_options(timeout=1, connect_timeout=2)
    assert operation.with_options(timeout=3).options == {
        "timeout": 3,
        "connect_timeout": 2,
    }


def test_a_layer_of_none_adds_nothing_to_a_dict_option():
    operation = GQLOperation().with_options(
        headers={"X-A": "1"}, httpx_options={"auth": _AUTH}
    )
    layered = operation.with_options(headers=None, httpx_options=None)
    assert layered.options == operation.options


def test_httpx_options_merge_by_key_and_replace_the_value():
    operation = (
        GQLOperation()
        .with_options(httpx_options={"auth": _AUTH, "extensions": {"a": 1, "b": 2}})
        .with_options(httpx_options={"follow_redirects": True})
        .with_options(httpx_options={"extensions": {"c": 3}})
    )
    assert operation.options == {
        "httpx_options": {
            "auth": _AUTH,
            "follow_redirects": True,
            "extensions": {"c": 3},
        }
    }


class _FalsyOptions(UserDict[str, object]):
    def __bool__(self) -> bool:
        return False


# Only `None` stands for an empty layer.
def test_a_falsy_mapping_of_httpx_options_is_read():
    operation = GQLOperation().with_options(httpx_options=_FalsyOptions(auth=_AUTH))
    assert operation.options == {"httpx_options": {"auth": _AUTH}}


def test_a_key_added_to_a_dict_later_does_not_reach_the_operation():
    headers = {"X-A": "1"}
    httpx_options: dict[str, object] = {"auth": _AUTH}
    operation = GQLOperation().with_options(
        headers=headers, httpx_options=httpx_options
    )
    headers["X-B"] = "2"
    httpx_options["follow_redirects"] = True
    assert operation.options == {
        "headers": {"X-A": "1"},
        "httpx_options": {"auth": _AUTH},
    }


def test_a_change_of_what_options_returned_does_not_reach_the_operation():
    operation = GQLOperation().with_options(
        timeout=1, headers={"X-A": "1"}, httpx_options={"auth": _AUTH}
    )
    options = operation.options
    headers = options.get("headers")
    assert headers is not None
    headers["X-B"] = "2"
    httpx_options = options.get("httpx_options")
    assert isinstance(httpx_options, MutableMapping)
    httpx_options["follow_redirects"] = True
    options["timeout"] = 2
    assert operation.options == {
        "timeout": 1,
        "headers": {"X-A": "1"},
        "httpx_options": {"auth": _AUTH},
    }


# --- what `with_options` refuses ---------------------------------------------


_LIMITS_HINT = (
    "use the options connect_timeout, timeout and stream_idle_timeout of the operation"
)
_OPERATION_HINT = "drop it, the client sends the operation itself"
_OWNED: dict[str, str] = {
    **dict.fromkeys(
        ("method", "url", "content", "data", "files", "json", "params"),
        _OPERATION_HINT,
    ),
    "headers": "use the option headers of the operation",
    "cookies": "drop it, the client keeps its cookies itself",
    "timeout": _LIMITS_HINT,
}
_OWNED_EXTENSION = f"'timeout' in 'extensions' ({_LIMITS_HINT})"


def _owned_message(*parts: str) -> str:
    return (
        "httpx_options of an operation holds keys the client sets itself: "
        f"{', '.join(parts)}"
    )


def _owned_parts(*keys: str) -> list[str]:
    return [f"{key!r} ({_OWNED[key]})" for key in keys]


def _foreign_message(*keys: str) -> str:
    listed = ", ".join(repr(key) for key in keys)
    return (
        f"httpx_options of an operation holds keys that a request does not take: "
        f"{listed}; it takes auth, extensions, follow_redirects: correct the "
        "spelling, or pass the settings of a connection to the client"
    )


def _unknown_message(*names: str) -> str:
    listed = ", ".join(repr(name) for name in names)
    return (
        f"An operation does not take the options {listed}; correct the spelling or "
        "drop them: it takes connect_timeout, headers, httpx_options, "
        "stream_idle_timeout, timeout"
    )


# Met as the operation is changed: by the way it is called, the checker's eyes
# aside.
def _refusal(options: Mapping[str, object]) -> Exception:
    untyped = cast("Callable[..., GQLOperation]", GQLOperation().with_options)
    with pytest.raises(Exception, match=r".") as refusal:
        untyped(**options)
    return refusal.value


def _httpx_refusal(httpx_options: Mapping[str, object]) -> Exception:
    return _refusal({"httpx_options": httpx_options})


@pytest.mark.parametrize("name", ["target_app", "base_url", "timeuot"])
def test_an_option_an_operation_does_not_take_is_refused(name: str):
    error = _refusal({name: None, "timeout": 1})
    assert type(error) is TypeError
    assert str(error) == _unknown_message(name)


def test_every_option_an_operation_does_not_take_is_named_at_once():
    error = _refusal({"timeuot": 1, "timeout": 1, "target_app": None})
    assert type(error) is TypeError
    assert str(error) == _unknown_message("timeuot", "target_app")


# The key is refused whatever its value.
@pytest.mark.parametrize("value", [None, "x"], ids=["none", "value"])
@pytest.mark.parametrize("key", list(_OWNED))
def test_a_key_the_client_sets_itself_is_refused(key: str, value: str | None):
    error = _httpx_refusal({"auth": _AUTH, key: value})
    assert type(error) is ValueError
    assert str(error) == _owned_message(*_owned_parts(key))


def test_a_timeout_among_the_extensions_is_refused():
    error = _httpx_refusal({"extensions": {"a": 1, "timeout": {"read": 1}}})
    assert type(error) is ValueError
    assert str(error) == _owned_message(_OWNED_EXTENSION)


def test_every_key_the_client_sets_itself_is_named_at_once():
    owned = dict.fromkeys(reversed(_OWNED))
    error = _httpx_refusal({**owned, "extensions": {"timeout": None}})
    assert type(error) is ValueError
    assert str(error) == _owned_message(*_owned_parts(*_OWNED), _OWNED_EXTENSION)


def test_every_key_a_request_does_not_take_is_named_at_once():
    error = _httpx_refusal({"verify": False, "auth": _AUTH, "atuh": _AUTH})
    assert type(error) is TypeError
    assert str(error) == _foreign_message("verify", "atuh")


def test_options_are_checked_by_name_then_by_owned_key_then_by_unknown_key():
    wrong: dict[str, object] = {"verify": False, "headers": {}}
    error = _refusal({"timeuot": 1, "httpx_options": wrong})
    assert str(error) == _unknown_message("timeuot")
    error = _httpx_refusal({"verify": False, "extensions": {"timeout": None}})
    assert str(error) == _owned_message(_OWNED_EXTENSION)
    error = _httpx_refusal(wrong)
    assert str(error) == _owned_message(*_owned_parts("headers"))


def test_a_binding_refuses_what_an_operation_does():
    bound = queries.get_node.bind(node=queries.thumbnail.with_args(width=1))
    untyped = cast("Callable[..., GQLOperation]", bound.with_options)
    with pytest.raises(TypeError, match="timeuot"):
        untyped(timeuot=1)


# The values are `httpx2`'s to judge, as the operation is executed.
@_every_mode
@_every_path
def test_a_value_httpx2_refuses_fails_the_execution(
    server: tuple[_Server, str], mode: _Mode, path: _Path
):
    _app, base_url = server
    change = _Change({"httpx_options": {"auth": 5}})
    with pytest.raises(TypeError, match='Invalid "auth" argument'):
        _execute(mode, path, base_url, {}, change)


# The call a generated module makes checks what reaches it without `with_options`.
async def test_options_given_to_the_client_itself_are_checked():
    wrong = GQLOperationOptions(httpx_options={"extensions": {"timeout": {}}})
    client = GQLClient(
        base_url="http://testserver/graphql", subscription_protocol="graphql-sse"
    )
    async_client = AsyncGQLClient(
        base_url="http://testserver/graphql", subscription_protocol="graphql-sse"
    )
    with pytest.raises(ValueError, match="'timeout' in 'extensions'"):
        client.query(pydantic.BaseModel, "query { ping }", variables={}, options=wrong)
    with pytest.raises(ValueError, match="'timeout' in 'extensions'"):
        await async_client.query(
            pydantic.BaseModel,
            "query { ping }",
            variables={},
            options=wrong,
        )
    client.close()
    await async_client.close()


# --- over the client's options -----------------------------------------------------


# Both operations reach the server, which takes `_DELAY` to answer: the first gets
# its answer, and the second does not wait for it.
@_every_mode
@pytest.mark.parametrize("path", ["query", "sse", "ws"])
@pytest.mark.parametrize(
    ("client_options", "first", "second"),
    [
        ({}, _Change(), _Change({"timeout": _SHORT})),
        ({"timeout": _SHORT}, _Change({"timeout": _LONG}), _Change()),
    ],
    ids=["shorter", "longer"],
)
def test_timeout_of_an_operation_goes_over_the_clients_for_it_alone(
    mode: _Mode,
    path: _Path,
    client_options: GQLClientOptions,
    first: _Change,
    second: _Change,
):
    app = _Server(delay=_DELAY)
    with live_asgi_server(app) as base_url, pytest.raises(TransportError):
        _execute(mode, path, base_url, client_options, first, second)
    assert len(app.seen) == 2


@_every_mode
def test_another_operation_waits_as_the_client_says(mode: _Mode):
    app = _Server(delay=_DELAY)
    steps: list[tuple[_Path, _Change]] = [
        ("query", _Change({"timeout": _LONG})),
        ("mutation", _Change()),
    ]
    with live_asgi_server(app) as base_url, pytest.raises(TransportError):
        _execute_all(mode, base_url, {"timeout": _SHORT}, steps)
    assert len(app.seen) == 2


# `use_client` sets a client with limits of its own: the operation's go over them.
@_every_mode
@_every_path
@pytest.mark.parametrize("level", ["client", "operation"])
def test_limits_reach_the_phases_of_the_request(
    server: tuple[_Server, str], mode: _Mode, path: _Path, level: str
):
    _app, base_url = server
    extensions = _Extensions(mode)
    limits = GQLOperationOptions(connect_timeout=3, timeout=4)
    if level == "client":
        options: GQLClientOptions = {**limits, "httpx_options": extensions.hook()}
        _execute(mode, path, base_url, options, _Change())
    else:
        options = {"timeout": 9, "httpx_options": extensions.hook()}
        _execute(mode, path, base_url, options, _Change(limits))
    assert [seen["timeout"] for seen in extensions.seen] == [
        {"connect": 3, "write": 3, "pool": 3, "read": 4}
    ]


# The limits of a client and an operation that name none, with a longer
# `timeout`, and with limits taken away.
@_every_mode
@_every_path
def test_each_limit_is_the_clients_until_the_operation_names_it(
    server: tuple[_Server, str], mode: _Mode, path: _Path
):
    _app, base_url = server
    extensions = _Extensions(mode)
    _execute(
        mode,
        path,
        base_url,
        {"httpx_options": extensions.hook()},
        _Change(),
        _Change({"timeout": 60}),
        _Change({"connect_timeout": None, "timeout": None}),
    )
    assert [seen["timeout"] for seen in extensions.seen] == [
        {"connect": 10, "write": 10, "pool": 10, "read": 10},
        {"connect": 10, "write": 10, "pool": 10, "read": 60},
        {"connect": None, "write": None, "pool": None, "read": None},
    ]


@_every_mode
@_every_path
@pytest.mark.parametrize(
    "change", [_Change, _ShortChange], ids=["with-options", "short-methods"]
)
def test_both_options_hold_whichever_is_set_first(
    server: tuple[_Server, str], mode: _Mode, path: _Path, change: type[_Change]
):
    app, base_url = server
    extensions = _Extensions(mode)
    timed = GQLOperationOptions(timeout=7)
    headed = GQLOperationOptions(headers={"X-A": "1"})
    _execute(
        mode,
        path,
        base_url,
        {"httpx_options": extensions.hook()},
        change(timed, headed),
        change(headed, timed),
    )
    assert app.header("x-a") == ["1", "1"]
    assert extensions.reads() == [7, 7]


@_every_mode
@_every_path
def test_two_with_headers_give_both_and_replace_a_name_in_another_case(
    server: tuple[_Server, str], mode: _Mode, path: _Path
):
    app, base_url = server
    change = _ShortChange(
        {"headers": {"X-First": "1", "X-Token": "1"}},
        {"headers": {"X-Second": "2", "x-TOKEN": "2"}},
    )
    _execute(mode, path, base_url, {}, change)
    assert app.header("x-first") == ["1"]
    assert app.header("x-second") == ["2"]
    assert app.header("x-token") == ["2"]


# The operation it was made of, and any other, wait as the client says.
@_every_mode
def test_limits_of_an_operation_hold_for_it_alone(
    server: tuple[_Server, str], mode: _Mode
):
    _app, base_url = server
    extensions = _Extensions(mode)
    _execute_all(
        mode,
        base_url,
        {"timeout": 9, "httpx_options": extensions.hook()},
        [
            ("query", _Change({"timeout": 7})),
            ("query", _Change()),
            ("mutation", _Change()),
            ("sse", _Change()),
        ],
    )
    assert extensions.reads() == [7, 9, 9, 9]


# One header on the wire for each name, whatever its spelling.
@_every_mode
@_every_path
def test_headers_merge_by_name_and_the_later_layer_wins(
    server: tuple[_Server, str], mode: _Mode, path: _Path
):
    app, base_url = server
    _execute(
        mode,
        path,
        base_url,
        {"headers": {"X-Client": "c", "X-Both": "c", "X-Twice": "c1", "x-twice": "c2"}},
        _Change(
            {"headers": {"X-First": "1", "X-Token": "1", "x-both": "o"}},
            {"headers": {"X-Second": "2", "x-TOKEN": "2"}},
            {"headers": {"X-Spelt": "a", "x-spelt": "b"}},
        ),
    )
    [seen] = app.seen
    assert {name: seen[name] for name in seen if name.startswith("x-")} == {
        "x-client": "c",
        "x-both": "o",
        "x-twice": "c2",
        "x-first": "1",
        "x-token": "2",
        "x-second": "2",
        "x-spelt": "b",
    }


@_every_mode
@_every_path
def test_auth_of_an_operation_goes_over_the_clients(
    server: tuple[_Server, str], mode: _Mode, path: _Path
):
    app, base_url = server
    _execute(
        mode, path, base_url, {}, _Change({"httpx_options": {"auth": _AUTH}}), _Change()
    )
    _execute(
        mode,
        path,
        base_url,
        {"httpx_options": {"auth": _AUTH}},
        _Change({"httpx_options": {"auth": ("other", "pw")}}),
        _Change({"httpx_options": {"auth": None}}),
        _Change(),
    )
    assert app.header("authorization") == [
        _AUTH_HEADER,
        None,
        "Basic b3RoZXI6cHc=",
        None,
        _AUTH_HEADER,
    ]


@_every_mode
@_every_path
def test_auth_of_an_operation_overwrites_its_authorization_header(
    server: tuple[_Server, str], mode: _Mode, path: _Path
):
    app, base_url = server
    change = _Change(
        {"headers": {"Authorization": "Bearer token"}},
        {"httpx_options": {"auth": _AUTH}},
    )
    _execute(mode, path, base_url, {}, change)
    assert app.header("authorization") == [_AUTH_HEADER]


@_every_mode
@_every_path
def test_follow_redirects_of_an_operation_goes_over_the_clients(
    server: tuple[_Server, str], httpserver: HTTPServer, mode: _Mode, path: _Path
):
    app, base_url = server
    httpserver.expect_request("/moved").respond_with_response(
        Response(status=307, headers={"Location": base_url})
    )
    moved = httpserver.url_for("/moved")
    followed = GQLOperationOptions(httpx_options={"follow_redirects": True})
    _execute(mode, path, moved, {}, _Change(followed))
    assert len(app.seen) == 1
    with pytest.raises(HTTPResponseError):
        _execute(
            mode,
            path,
            moved,
            {"httpx_options": {"follow_redirects": True}},
            _Change({"httpx_options": {"follow_redirects": False}}),
        )


# The limits travel as an extension too, beside those of the operation.
@_every_mode
@_every_path
def test_extensions_of_an_operation_reach_its_request(
    server: tuple[_Server, str], mode: _Mode, path: _Path
):
    _app, base_url = server
    extensions = _Extensions(mode)
    own = {"x-note": "mine"}
    limits = {"connect": 10, "write": 10, "pool": 10, "read": 10}
    _execute(
        mode,
        path,
        base_url,
        {"httpx_options": extensions.hook()},
        _Change({"httpx_options": {"extensions": own}}),
        _Change({"httpx_options": {"extensions": None}}),
    )
    assert extensions.seen == [
        {"x-note": "mine", "timeout": limits},
        {"timeout": limits},
    ]
    assert own == {"x-note": "mine"}


@_every_mode
def test_options_of_a_binding_keep_its_fragment_arguments(
    server: tuple[_Server, str], mode: _Mode
):
    app, base_url = server
    change = _Change({"headers": {"X-A": "1"}}, {"timeout": 5})
    _execute(mode, "binding", base_url, {}, change)
    assert app.header("x-a") == ["1"]
    assert app.variables == [{"width": 7}]


# Options set where an operation is declared hold with those set at a call.
@_every_mode
def test_options_of_a_declaration_hold_at_a_call(
    server: tuple[_Server, str], mode: _Mode
):
    app, base_url = server
    extensions = _Extensions(mode)
    declared = sync_queries.patient if mode == "sync" else queries.patient
    assert declared.options == {"timeout": 60}
    _execute(
        mode,
        "declared",
        base_url,
        {"httpx_options": extensions.hook()},
        _Change({"headers": {"X-A": "1"}}),
        _Change({"timeout": 7}),
    )
    assert app.header("x-a") == ["1", None]
    assert extensions.reads() == [60, 7]


# The README's own example, on a generated operation.
async def test_readme_example_refines_the_options_of_an_operation(
    server: tuple[_Server, str],
):
    app, base_url = server
    namespace: dict[str, object] = {
        "GetUserQueryResult": api.GetUserQueryResult,
        "get_user": queries.get_user,
    }
    exec(readme_block("get_user_slowly"), namespace)
    get_user_slowly = cast(
        "Callable[[str, str], Awaitable[api.GetUserQueryResult]]",
        namespace["get_user_slowly"],
    )
    extensions = _Extensions("async")
    async with use_package_client(
        "operation_options", base_url, httpx_options=extensions.hook()
    ):
        result = await get_user_slowly("1", "token")
    assert result.user is not None
    assert result.user.id == "1"
    assert app.variables == [{"id": "1"}]
    assert app.header("authorization") == ["token"]
    assert extensions.reads() == [60]


# A client set on the module gives its own defaults, and the module's own client
# gives none: the options of the operation hold on either.
@_every_mode
def test_options_of_an_operation_hold_on_a_client_set_by_use_client(
    server: tuple[_Server, str], mode: _Mode
):
    app, base_url = server
    _execute(
        mode,
        "query",
        base_url,
        {"headers": {"X-Client": "c"}, "timeout": _SHORT},
        _Change({"headers": {"X-A": "1"}, "timeout": _LONG}),
    )
    assert app.header("x-client") == ["c"]
    assert app.header("x-a") == ["1"]
