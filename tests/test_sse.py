import asyncio
import socket
import struct
import threading
import time
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import cast

import httpx2
import pydantic
import pytest

from iron_gql import FileVar
from iron_gql import GraphQLResponseError
from iron_gql import SubscriptionInterruptedError
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.runtime import SubscriptionProtocol
from iron_gql.testing.server import async_live_asgi_server
from iron_gql.testing.server import live_asgi_server
from tests.conftest import ProjectBuilder
from tests.conftest import SSETestRequest
from tests.conftest import accept_graphql_sse
from tests.conftest import generated_package
from tests.conftest import make_sse_subscription_app
from tests.conftest import make_subscription_app
from tests.conftest import use_package_client

generated_package(
    "sse_codegen",
    schema="""
    type Query {
        _dummy: String
    }

    type Subscription {
        events(channel: String!): Event!
    }

    type Event {
        id: ID!
        message: String!
    }
    """,
    queries='''
    from tests.generated.sse_codegen.gql.api import api_gql

    events = api_gql(
        """
        subscription Events($channel: String!) {
            events(channel: $channel) {
                id
                message
            }
        }
        """
    )
    ''',
)

from tests.generated.sse_codegen import queries as codegen_queries

# Long enough for any healthy step of a test, short enough that a client which
# waits for the end of the response fails the test instead of hanging it.
_STEP_TIMEOUT = 5

_SUBSCRIPTION = "subscription { counter }"

type _Handler = Callable[[SSETestRequest], Awaitable[None]]


class _CounterResult(pydantic.BaseModel):
    counter: int


def _counter(value: int) -> dict[str, object]:
    return {"data": {"counter": value}}


def _sse_app(handler: _Handler) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        await handler(await accept_graphql_sse(scope, receive, send))

    return app


@asynccontextmanager
async def _sse_client(
    handler: _Handler, subscription_idle_timeout: float | None = None
) -> AsyncIterator[AsyncGQLClient]:
    async with async_live_asgi_server(_sse_app(handler)) as base_url:
        client = AsyncGQLClient(
            subscription_protocol="graphql-sse",
            base_url=base_url,
            subscription_idle_timeout=subscription_idle_timeout,
        )
        try:
            yield client
        finally:
            await client.close()


async def _read_counters(client: AsyncGQLClient, into: list[int]) -> None:
    async with client.subscribe(
        _CounterResult, _SUBSCRIPTION, variables={}, headers={}
    ) as stream:
        async for item in stream:
            into.append(item.counter)  # noqa: PERF401


async def test_events_arrive_while_the_response_is_still_open():
    # The server sends the second event only after the client has handed the
    # first one to the caller. A client that waits for the end of the
    # response before yielding anything never gets there.
    first_delivered = asyncio.Event()

    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))
        await first_delivered.wait()
        await request.next(_counter(2))
        await request.complete()
        await request.end()

    async with _sse_client(handler) as client, asyncio.timeout(_STEP_TIMEOUT):
        counters: list[int] = []
        async with client.subscribe(
            _CounterResult, _SUBSCRIPTION, variables={}, headers={}
        ) as stream:
            async for item in stream:
                counters.append(item.counter)
                first_delivered.set()
        assert counters == [1, 2]


async def test_complete_ends_the_stream_without_waiting_for_the_response_end():
    # `complete` is the protocol's end of the operation; the HTTP response may
    # stay open behind it. Leaving the subscription then hangs up, which is
    # how the server learns the client is gone.
    disconnected = asyncio.Event()

    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))
        await request.complete()
        assert await request.wait_disconnect() == "http.disconnect"
        disconnected.set()

    async with _sse_client(handler) as client, asyncio.timeout(_STEP_TIMEOUT):
        counters: list[int] = []
        await _read_counters(client, counters)
        assert counters == [1]
        await disconnected.wait()


async def test_leaving_the_subscription_early_closes_the_connection():
    disconnected = asyncio.Event()

    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))
        assert await request.wait_disconnect() == "http.disconnect"
        disconnected.set()

    async with _sse_client(handler) as client, asyncio.timeout(_STEP_TIMEOUT):
        async with client.subscribe(
            _CounterResult, _SUBSCRIPTION, variables={}, headers={}
        ) as stream:
            async for item in stream:
                assert item.counter == 1
                break
        await disconnected.wait()


class _PingResult(pydantic.BaseModel):
    ping: str


async def test_request_is_a_post_that_asks_for_an_event_stream():
    # The cookie is set by a query's response: the subscription goes out
    # through the same client, with its default headers and its cookies.
    subscriptions: list[SSETestRequest] = []

    async def handler(request: SSETestRequest) -> None:
        if request.headers["accept"] != "text/event-stream":
            await request.send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"set-cookie", b"session=abc; Path=/"),
                ],
            })
            await request.send({
                "type": "http.response.body",
                "body": b'{"data": {"ping": "pong"}}',
            })
            return
        subscriptions.append(request)
        await request.open_stream()
        await request.complete()
        await request.end()

    async with async_live_asgi_server(_sse_app(handler)) as base_url:
        client = AsyncGQLClient(
            subscription_protocol="graphql-sse",
            base_url=base_url,
            headers={"X-Default": "on"},
        )
        try:
            await client.query(_PingResult, "query { ping }", variables={}, headers={})
            async with client.subscribe(
                _CounterResult,
                "subscription ($n: Int!) { counter(n: $n) }",
                variables={"n": 3},
                headers={"X-Call": "yes"},
            ) as stream:
                assert [item async for item in stream] == []
        finally:
            await client.close()

    [request] = subscriptions
    assert request.scope["method"] == "POST"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-default"] == "on"
    assert request.headers["x-call"] == "yes"
    assert request.headers["cookie"] == "session=abc"
    assert request.body == {
        "query": "subscription ($n: Int!) { counter(n: $n) }",
        "variables": {"n": 3},
    }


async def test_generated_subscription_streams_typed_models():
    async with (
        async_live_asgi_server(
            make_sse_subscription_app([
                {"data": {"events": {"id": "1", "message": "hello"}}},
                {"data": {"events": {"id": "2", "message": "world"}}},
            ])
        ) as base_url,
        use_package_client("sse_codegen", base_url),
        codegen_queries.events.execute(channel="test") as stream,
    ):
        results = [(item.events.id, item.events.message) async for item in stream]
    assert results == [("1", "hello"), ("2", "world")]


@pytest.mark.parametrize("protocol", ["graphql-sse", "graphql-transport-ws"])
async def test_generated_module_subscribes_by_the_protocol_it_was_generated_with(
    test_project: ProjectBuilder, protocol: SubscriptionProtocol
):
    # The module's own client, not one swapped in: the protocol given to the
    # generator is the one its subscriptions speak.
    app = (
        make_sse_subscription_app([{"data": {"counter": 7}}])
        if protocol == "graphql-sse"
        else make_subscription_app([
            {"type": "next", "payload": {"data": {"counter": 7}}},
            {"type": "complete"},
        ])
    )
    async with async_live_asgi_server(app) as base_url:
        test_project.prepare(
            schema="""
            type Query { _dummy: String }
            type Subscription { counter: Int! }
            """,
            queries="""
            from sample_app.gql.api import api_gql

            counter = api_gql("subscription Counter { counter }")
            """,
            base_url=base_url,
        )
        api_module, _queries_module = test_project.generate_and_import(protocol)
        client = cast("AsyncGQLClient", vars(api_module)["_client"])
        try:
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                assert [item.counter async for item in stream] == [7]
        finally:
            await client.close()


async def test_refusal_before_the_stream_raises_http_status_error_with_its_body():
    async def handler(request: SSETestRequest) -> None:
        await request.respond(
            401,
            "application/graphql-response+json",
            b'{"errors": [{"message": "Login required"}]}',
        )

    async with _sse_client(handler) as client:
        with pytest.raises(httpx2.HTTPStatusError) as error:
            await _read_counters(client, [])
    assert error.value.response.status_code == 401
    assert error.value.response.json() == {"errors": [{"message": "Login required"}]}


async def test_single_graphql_response_with_errors_raises_them():
    async def handler(request: SSETestRequest) -> None:
        await request.respond(
            200, "application/json", b'{"errors": [{"message": "Cannot query"}]}'
        )

    async with _sse_client(handler) as client:
        with pytest.raises(GraphQLResponseError, match="Cannot query"):
            await _read_counters(client, [])


async def test_single_graphql_response_without_errors_is_not_a_stream():
    async def handler(request: SSETestRequest) -> None:
        await request.respond(200, "application/json", b'{"data": {"counter": 1}}')

    async with _sse_client(handler) as client:
        with pytest.raises(
            GraphQLResponseError,
            match="Expected an event stream, got 'application/json'",
        ):
            await _read_counters(client, [])


async def test_next_with_errors_ends_the_stream_with_them():
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))
        await request.next({"data": None, "errors": [{"message": "revoked"}]})
        await request.complete()
        await request.end()

    counters: list[int] = []
    async with _sse_client(handler) as client:
        with pytest.raises(GraphQLResponseError, match="revoked"):
            await _read_counters(client, counters)
    assert counters == [1]


async def test_next_without_data_or_errors_is_an_error():
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next({})
        await request.end()

    async with _sse_client(handler) as client:
        with pytest.raises(GraphQLResponseError, match="No data in response"):
            await _read_counters(client, [])


async def test_next_with_malformed_data_is_an_error():
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.write(b"event: next\ndata: {not json\n\n")
        await request.end()

    async with _sse_client(handler) as client:
        with pytest.raises(GraphQLResponseError, match="Malformed next payload"):
            await _read_counters(client, [])


async def test_result_failing_validation_passes_through_as_validation_error():
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next({"data": {"counter": "many"}})
        await request.end()

    async with _sse_client(handler) as client:
        with pytest.raises(pydantic.ValidationError, match="counter"):
            await _read_counters(client, [])


async def test_oversized_event_is_not_an_interruption():
    # httpx2 caps one event at 1 MiB and raises its `SSEError`, a
    # `TransportError` like the interruptions -- but nothing broke, so it is
    # not reported as one.
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.write(b"event: next\ndata: " + b"x" * (1024 * 1024 + 1))
        await request.write(b"\n\n")
        await request.end()

    async with _sse_client(handler) as client:
        with pytest.raises(httpx2.SSEError, match="byte limit"):
            await _read_counters(client, [])


async def test_event_of_another_type_is_an_error():
    # An event without an `event:` field is a plain "message" to SSE; the
    # protocol has no use for one.
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.write(b'data: {"data": {"counter": 1}}\n\n')
        await request.end()

    async with _sse_client(handler) as client:
        with pytest.raises(
            GraphQLResponseError, match="Unexpected subscription event event: 'message'"
        ):
            await _read_counters(client, [])


async def test_events_framed_with_crlf_and_heartbeat_comments_are_read():
    # The framing Strawberry writes: CRLF line ends, an empty `data` on
    # `complete`, and `: ping` comments between events.
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.write(b": ping\r\n\r\n")
        await request.write(b'event: next\r\ndata: {"data": {"counter": 1}}\r\n\r\n')
        await request.write(b": ping\r\n\r\n")
        await request.write(b"event: complete\r\ndata: \r\n\r\n")
        await request.end()

    counters: list[int] = []
    async with _sse_client(handler) as client:
        await _read_counters(client, counters)
    assert counters == [1]


async def test_response_ending_without_complete_is_an_interruption():
    # Raised, not retried: whether reopening the subscription makes sense is
    # the caller's to decide, so the server sees exactly one request.
    requests: list[SSETestRequest] = []

    async def handler(request: SSETestRequest) -> None:
        requests.append(request)
        await request.open_stream()
        await request.next(_counter(1))
        await request.end()

    counters: list[int] = []
    async with _sse_client(handler) as client:
        with pytest.raises(
            SubscriptionInterruptedError, match="Event stream ended without complete"
        ):
            await _read_counters(client, counters)
    assert counters == [1]
    assert len(requests) == 1


async def test_query_timeout_does_not_limit_the_wait_between_events():
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await asyncio.sleep(1.5)
        await request.next(_counter(1))
        await request.complete()
        await request.end()

    async with async_live_asgi_server(_sse_app(handler)) as base_url:
        client = AsyncGQLClient(
            subscription_protocol="graphql-sse", base_url=base_url, query_timeout=1
        )
        counters: list[int] = []
        try:
            await _read_counters(client, counters)
        finally:
            await client.close()
    assert counters == [1]


async def test_response_broken_mid_body_is_an_interruption():
    # The app returns without finishing its response: uvicorn drops the
    # connection in the middle of the chunked body.
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))

    counters: list[int] = []
    async with _sse_client(handler) as client:
        with pytest.raises(SubscriptionInterruptedError) as error:
            await _read_counters(client, counters)
    assert counters == [1]
    assert isinstance(error.value.__cause__, httpx2.RemoteProtocolError)


_SOCKET_ADDRESS = pydantic.TypeAdapter(tuple[str, int])

_RESET_RESPONSE_HEAD = b"\r\n".join((
    b"HTTP/1.1 200 OK",
    b"Content-Type: text/event-stream",
    b"Transfer-Encoding: chunked",
    b"",
    b"",
))


# Answers one request with a stream holding a single event, then resets the
# connection: SO_LINGER with a zero timeout makes close() send RST instead of
# FIN. uvicorn has no way to abort a connection like that.
def _reset_after_first_event(listener: socket.socket) -> None:
    connection = listener.accept()[0]
    with connection:
        request = b""
        while b"\r\n\r\n" not in request:
            request += connection.recv(4096)
        event = b'event: next\ndata: {"data": {"counter": 1}}\n\n'
        chunk = f"{len(event):x}\r\n".encode() + event + b"\r\n"
        connection.sendall(_RESET_RESPONSE_HEAD + chunk)
        time.sleep(0.2)
        connection.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )


async def test_connection_reset_mid_stream_is_an_interruption():
    # A reset rather than a close: the pending read fails instead of seeing
    # the body end.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        _host, port = _SOCKET_ADDRESS.validate_python(listener.getsockname())
        server = threading.Thread(target=_reset_after_first_event, args=(listener,))
        server.start()
        client = AsyncGQLClient(
            subscription_protocol="graphql-sse", base_url=f"http://127.0.0.1:{port}/"
        )
        counters: list[int] = []
        try:
            with pytest.raises(SubscriptionInterruptedError) as error:
                await _read_counters(client, counters)
        finally:
            await client.close()
            server.join(_STEP_TIMEOUT)
    assert counters == [1]
    assert isinstance(error.value.__cause__, httpx2.ReadError)


async def test_silence_past_the_idle_timeout_is_an_interruption():
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))
        await request.wait_disconnect()

    counters: list[int] = []
    async with _sse_client(handler, subscription_idle_timeout=0.2) as client:
        with pytest.raises(SubscriptionInterruptedError) as error:
            await _read_counters(client, counters)
    assert counters == [1]
    assert isinstance(error.value.__cause__, httpx2.ReadTimeout)


async def test_heartbeat_comments_keep_a_quiet_stream_within_the_idle_timeout():
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        for _ in range(8):
            await asyncio.sleep(0.05)
            await request.write(b": ping\n\n")
        await request.next(_counter(1))
        await request.complete()
        await request.end()

    counters: list[int] = []
    async with _sse_client(handler, subscription_idle_timeout=0.2) as client:
        await _read_counters(client, counters)
    assert counters == [1]


async def test_waiting_for_the_response_headers_is_bounded_by_the_idle_timeout():
    # The stream is not accepted yet, so this is the transport's own timeout,
    # not an interruption of a subscription.
    async def handler(request: SSETestRequest) -> None:
        await request.wait_disconnect()

    async with _sse_client(handler, subscription_idle_timeout=0.2) as client:
        with pytest.raises(httpx2.ReadTimeout):
            await _read_counters(client, [])


async def test_in_process_target_app_is_refused_for_sse():
    client = AsyncGQLClient(
        subscription_protocol="graphql-sse",
        base_url="http://testserver/graphql",
        target_app=make_sse_subscription_app([]),
    )
    try:
        with pytest.raises(ValueError, match="async_live_asgi_server"):
            await _read_counters(client, [])
    finally:
        await client.close()


async def test_file_variables_are_refused():
    client = AsyncGQLClient(
        subscription_protocol="graphql-sse", base_url="http://testserver/graphql"
    )
    try:
        with pytest.raises(TypeError, match="File uploads"):
            async with client.subscribe(
                _CounterResult,
                _SUBSCRIPTION,
                variables={"file": FileVar(b"x")},
                headers={},
            ):
                pass
    finally:
        await client.close()


# The sync client reads the same stream through its own code path: what
# matters there is that it yields, stops and fails the way the async one does.


def _sync_counters(base_url: str) -> list[int]:
    client = GQLClient(subscription_protocol="graphql-sse", base_url=base_url)
    try:
        with client.subscribe(
            _CounterResult, _SUBSCRIPTION, variables={}, headers={}
        ) as stream:
            return [item.counter for item in stream]
    finally:
        client.close()


def test_sync_events_arrive_while_the_response_is_still_open():
    first_delivered = threading.Event()

    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))
        await asyncio.to_thread(first_delivered.wait, _STEP_TIMEOUT)
        await request.next(_counter(2))
        await request.complete()
        await request.end()

    with live_asgi_server(_sse_app(handler)) as base_url:
        client = GQLClient(subscription_protocol="graphql-sse", base_url=base_url)
        try:
            counters: list[int] = []
            with client.subscribe(
                _CounterResult, _SUBSCRIPTION, variables={}, headers={}
            ) as stream:
                for item in stream:
                    counters.append(item.counter)
                    first_delivered.set()
        finally:
            client.close()
    assert counters == [1, 2]


def test_sync_leaving_the_subscription_early_closes_the_connection():
    disconnected = threading.Event()

    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))
        assert await request.wait_disconnect() == "http.disconnect"
        disconnected.set()

    with live_asgi_server(_sse_app(handler)) as base_url:
        client = GQLClient(subscription_protocol="graphql-sse", base_url=base_url)
        try:
            with client.subscribe(
                _CounterResult, _SUBSCRIPTION, variables={}, headers={}
            ) as stream:
                for item in stream:
                    assert item.counter == 1
                    break
        finally:
            client.close()
        assert disconnected.wait(_STEP_TIMEOUT)


def test_sync_refusal_before_the_stream_raises_http_status_error_with_its_body():
    async def handler(request: SSETestRequest) -> None:
        await request.respond(
            401,
            "application/graphql-response+json",
            b'{"errors": [{"message": "Login required"}]}',
        )

    with (
        live_asgi_server(_sse_app(handler)) as base_url,
        pytest.raises(httpx2.HTTPStatusError) as error,
    ):
        _sync_counters(base_url)
    assert error.value.response.status_code == 401
    assert error.value.response.json() == {"errors": [{"message": "Login required"}]}


def test_sync_response_ending_without_complete_is_an_interruption():
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))
        await request.end()

    with (
        live_asgi_server(_sse_app(handler)) as base_url,
        pytest.raises(
            SubscriptionInterruptedError, match="Event stream ended without complete"
        ),
    ):
        _sync_counters(base_url)


def test_sync_response_broken_mid_body_is_an_interruption():
    async def handler(request: SSETestRequest) -> None:
        await request.open_stream()
        await request.next(_counter(1))

    with (
        live_asgi_server(_sse_app(handler)) as base_url,
        pytest.raises(SubscriptionInterruptedError) as error,
    ):
        _sync_counters(base_url)
    assert isinstance(error.value.__cause__, httpx2.RemoteProtocolError)
