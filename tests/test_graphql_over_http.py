"""How the client reads a response by its status and media type, and which
`Accept` it sends, under GraphQL over HTTP.

The responses come from the corpus (`tests/corpus/graphql_over_http.py`), and
what the client must make of each is the table of the ticket that decided the
rule. Every case goes through a generated operation -- `with_headers()` then
`execute()` -- in both modes, for a query's response and for a subscription's
response in place of its stream: the one rule covers both.
"""

from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import asynccontextmanager
from contextlib import contextmanager

import httpx2
import pydantic
import pytest

from iron_gql import GraphQLResponseError
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.testing import use_client
from iron_gql.testing.server import live_asgi_server
from tests.conftest import SSETestRequest
from tests.conftest import accept_graphql_sse
from tests.conftest import generated_package
from tests.corpus.graphql_over_http import CLIENT_ACCEPTS
from tests.corpus.graphql_over_http import DATA
from tests.corpus.graphql_over_http import AcceptCase
from tests.corpus.graphql_over_http import Outcome
from tests.corpus.graphql_over_http import ResponseCase
from tests.corpus.graphql_over_http import build_accept_corpus
from tests.corpus.graphql_over_http import build_corpus
from tests.corpus.graphql_over_http import expected
from tests.corpus.graphql_over_http import expected_accept

_SCHEMA = """
type Query {
    ping: String!
}

type Subscription {
    counter: Int!
}
"""


def _queries(package: str) -> str:
    return f"""
    from tests.generated.{package}.gql.api import api_gql

    ping = api_gql("query Ping {{ ping }}")
    counter = api_gql("subscription Counter {{ counter }}")
    """


generated_package(
    "graphql_over_http", schema=_SCHEMA, queries=_queries("graphql_over_http")
)
generated_package(
    "graphql_over_http_sync",
    schema=_SCHEMA,
    queries=_queries("graphql_over_http_sync"),
    mode="sync",
)

from tests.generated.graphql_over_http import queries as async_queries
from tests.generated.graphql_over_http.gql import api as async_api
from tests.generated.graphql_over_http_sync import queries as sync_queries
from tests.generated.graphql_over_http_sync.gql import api as sync_api

CORPUS = build_corpus()
ACCEPT_CORPUS = build_accept_corpus()
_CASES = {case.id: case for case in CORPUS}

# The header that names the corpus case a request asks the fake to answer
# with; a request without it is answered with success for its operation.
_CASE_HEADER = "X-Case"

# Each request's headers, under the case it named ("" for none).
type _Seen = dict[str, list[dict[str, str]]]


async def _succeed(request: SSETestRequest) -> None:
    if str(request.body["query"]).startswith("subscription"):
        await request.open_stream()
        await request.next({"data": {"counter": 1}})
        await request.complete()
        await request.end()
    else:
        await request.respond(200, "application/json", b'{"data": {"ping": "pong"}}')


async def _answer(request: SSETestRequest, case: ResponseCase) -> None:
    headers: list[tuple[bytes, bytes]] = []
    if case.media_type.header is not None:
        headers.append((b"content-type", case.media_type.header.encode()))
    if case.status.status_class == "redirect":
        # Back at the endpoint: a client that followed it would ask again
        # rather than raise on the first answer.
        headers.append((b"location", b"/graphql"))
    await request.send({
        "type": "http.response.start",
        "status": case.status.code,
        "headers": headers,
    })
    await request.send({"type": "http.response.body", "body": case.body.content})


def _server(seen: _Seen) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        request = await accept_graphql_sse(scope, receive, send)
        case_id = request.headers.get(_CASE_HEADER.lower(), "")
        seen.setdefault(case_id, []).append(request.headers)
        if case_id:
            await _answer(request, _CASES[case_id])
        else:
            await _succeed(request)

    return app


# What a call ended with, in the corpus's terms: the outcome it matches, or a
# description of what came instead.
def _observed(
    case: ResponseCase, result: object, requests: list[dict[str, str]]
) -> str:
    match result:
        case pydantic.BaseModel() if result.model_dump() == DATA:
            return "data"
        case GraphQLResponseError() if str(result).startswith(
            "Malformed response body"
        ):
            return "malformed"
        case GraphQLResponseError() if str(result) == "No data in response":
            return "no-data"
        case GraphQLResponseError() if str(result).startswith(
            "Expected an event stream"
        ):
            return "not-a-stream"
        case GraphQLResponseError() if (
            case.body.errors is not None and result.errors == case.body.errors.value
        ):
            return "errors"
        case httpx2.HTTPStatusError() if (
            str(result) == "Unexpected 3xx response (307) to /graphql"
            and len(requests) == 1
        ):
            return "redirect"
        case httpx2.HTTPStatusError() if (
            result.response.status_code == case.status.code
            and result.response.content == case.body.content
            and case.status.status_class != "redirect"
        ):
            return "status"
        case _:
            return (
                f"{type(result).__name__}: {result!r} after {len(requests)} request(s)"
            )


def _divergence(
    case: ResponseCase, *, subscription: bool, result: object, seen: _Seen
) -> str | None:
    want: Outcome | None = expected(case, subscription=subscription)
    if want is None:
        return None
    got = _observed(case, result, seen.get(case.id, []))
    return None if got == want else f"{case.id}: expected {want}, got {got}"


def _accept_divergence(
    case: AcceptCase, *, subscription: bool, seen: _Seen
) -> str | None:
    want = expected_accept(case, subscription=subscription)
    got = seen[""][-1]["accept"]
    return None if got == want else f"{case.id}: expected {want!r}, got {got!r}"


type _AsyncCall = Callable[[dict[str, str]], Awaitable[object]]
type _SyncCall = Callable[[dict[str, str]], object]


async def _async_query(headers: dict[str, str]) -> object:
    return await async_queries.ping.with_headers(headers).execute()


async def _async_subscribe(headers: dict[str, str]) -> object:
    async with async_queries.counter.with_headers(headers).execute() as stream:
        return [item.counter async for item in stream]


def _sync_query(headers: dict[str, str]) -> object:
    return sync_queries.ping.with_headers(headers).execute()


def _sync_subscribe(headers: dict[str, str]) -> object:
    with sync_queries.counter.with_headers(headers).execute() as stream:
        return [item.counter for item in stream]


_ASYNC_PATHS: dict[str, _AsyncCall] = {
    "query": _async_query,
    "subscription": _async_subscribe,
}
_SYNC_PATHS: dict[str, _SyncCall] = {
    "query": _sync_query,
    "subscription": _sync_subscribe,
}


@asynccontextmanager
async def _async_module(
    seen: _Seen, client_headers: dict[str, str] | None = None
) -> AsyncIterator[None]:
    with live_asgi_server(_server(seen)) as base_url:
        client = AsyncGQLClient(
            base_url=base_url,
            subscription_protocol="graphql-sse",
            headers=client_headers,
        )
        async with use_client(async_api, client):
            yield


@contextmanager
def _sync_module(
    seen: _Seen, client_headers: dict[str, str] | None = None
) -> Iterator[None]:
    with live_asgi_server(_server(seen)) as base_url:
        client = GQLClient(
            base_url=base_url,
            subscription_protocol="graphql-sse",
            headers=client_headers,
        )
        with use_client(sync_api, client):
            yield


async def _async_result(call: _AsyncCall, headers: dict[str, str]) -> object:
    try:
        return await call(headers)
    except Exception as exc:  # noqa: BLE001 -- judged against the table
        return exc


def _sync_result(call: _SyncCall, headers: dict[str, str]) -> object:
    try:
        return call(headers)
    except Exception as exc:  # noqa: BLE001 -- judged against the table
        return exc


def test_corpus_ids_are_unique():
    assert len(_CASES) == len(CORPUS)
    assert len({case.id for case in ACCEPT_CORPUS}) == len(ACCEPT_CORPUS)


@pytest.mark.parametrize("operation", list(_ASYNC_PATHS))
async def test_async_module_reads_every_response_by_the_table(operation: str):
    call = _ASYNC_PATHS[operation]
    seen: _Seen = {}
    divergences: list[str] = []
    async with _async_module(seen):
        for case in CORPUS:
            result = await _async_result(call, {_CASE_HEADER: case.id})
            divergence = _divergence(
                case,
                subscription=operation == "subscription",
                result=result,
                seen=seen,
            )
            if divergence is not None:
                divergences.append(divergence)
    assert divergences == []


@pytest.mark.parametrize("operation", list(_SYNC_PATHS))
def test_sync_module_reads_every_response_by_the_table(operation: str):
    call = _SYNC_PATHS[operation]
    seen: _Seen = {}
    divergences: list[str] = []
    with _sync_module(seen):
        for case in CORPUS:
            result = _sync_result(call, {_CASE_HEADER: case.id})
            divergence = _divergence(
                case,
                subscription=operation == "subscription",
                result=result,
                seen=seen,
            )
            if divergence is not None:
                divergences.append(divergence)
    assert divergences == []


@pytest.mark.parametrize("operation", list(_ASYNC_PATHS))
async def test_async_module_sends_accept_by_the_table(operation: str):
    call = _ASYNC_PATHS[operation]
    divergences: list[str] = []
    for client in CLIENT_ACCEPTS:
        seen: _Seen = {}
        async with _async_module(seen, dict(client.headers)):
            for case in (c for c in ACCEPT_CORPUS if c.client == client):
                await call(dict(case.call.headers))
                divergence = _accept_divergence(
                    case, subscription=operation == "subscription", seen=seen
                )
                if divergence is not None:
                    divergences.append(divergence)
    assert divergences == []


@pytest.mark.parametrize("operation", list(_SYNC_PATHS))
def test_sync_module_sends_accept_by_the_table(operation: str):
    call = _SYNC_PATHS[operation]
    divergences: list[str] = []
    for client in CLIENT_ACCEPTS:
        seen: _Seen = {}
        with _sync_module(seen, dict(client.headers)):
            for case in (c for c in ACCEPT_CORPUS if c.client == client):
                call(dict(case.call.headers))
                divergence = _accept_divergence(
                    case, subscription=operation == "subscription", seen=seen
                )
                if divergence is not None:
                    divergences.append(divergence)
    assert divergences == []
