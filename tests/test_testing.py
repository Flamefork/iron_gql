import importlib
import socket
import sys
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from typing import cast
from urllib.parse import urlsplit

import httpx2
import pydantic
import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.testing import use_client
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


def _resolve_ping(_root: None, _info: GraphQLResolveInfo) -> str:
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
    test_project.gql_pkg = "sample_app.gql.reports"
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
    client = AsyncGQLClient(base_url=_ping_url(httpserver))

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
    client = GQLClient(base_url=_ping_url(httpserver))

    # `ping` was constructed at module import, before the client is replaced.
    with use_client(api_module, client) as active:
        assert active is client
        assert namespace["_client"] is client
        result = sync_swap_queries.ping.execute()
        assert result.ping == "pong"

    assert namespace["_client"] is original
    with pytest.raises(RuntimeError, match="closed"):
        client.query(_PingResult, "query Ping { ping }", variables={}, headers={})


def test_live_asgi_server_defaults_to_the_graphql_path():
    with live_asgi_server(_ok_app) as base_url:
        assert base_url.endswith("/graphql")


def test_live_asgi_server_serves_the_given_path_and_frees_the_port():
    with live_asgi_server(_ok_app, path="/custom") as base_url:
        assert base_url.endswith("/custom")
        response = httpx2.post(base_url, json={"query": "query Ping { ping }"})
        assert response.json() == {"data": {"ping": "pong"}}

    port = urlsplit(base_url).port
    assert port is not None
    with socket.socket() as probe:
        probe.settimeout(2)
        with pytest.raises(ConnectionRefusedError):
            probe.connect(("127.0.0.1", port))


def test_live_asgi_server_reraises_a_failed_startup():
    with pytest.raises(ValueError, match="broken app"), live_asgi_server(_broken_app):
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
