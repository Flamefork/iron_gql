"""What an operation without a result raises: the error model of ADR 0009.

Every failure is met through a public client, in both modes. What a GraphQL
server answers over HTTP is the corpus's (`tests/test_graphql_over_http.py`);
here are the failures no ASGI app can produce -- a connection that breaks, a
body that cannot be read, a close frame -- and what must not be taken for one.
"""

import asyncio
import contextlib
import gzip
import json
import re
import socket
import struct
import threading
import time
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from typing import cast
from typing import override

import httpx2
import pydantic
import pytest
import wsproto
import wsproto.events
from httpx2.websockets import AsyncWebSocketClient
from httpx2.websockets import AsyncWebSocketSession
from httpx2.websockets import WebSocketDisconnect
from httpx2.websockets import WebSocketInvalidTypeReceived
from httpx2.websockets import WebSocketNetworkError
from httpx2.websockets import WebSocketSession
from httpx2.websockets import WebSocketUpgradeError
from wsproto.frame_protocol import CloseReason

import iron_gql
from iron_gql import GraphQLClientError
from iron_gql import GraphQLResponseError
from iron_gql import HTTPResponseError
from iron_gql import ResponseError
from iron_gql import ResponseProtocolError
from iron_gql import SubscriptionInterruptedError
from iron_gql import TransportError
from iron_gql import WebSocketClosedError
from iron_gql import errors
from iron_gql import runtime
from iron_gql import websockets
from iron_gql.protocol import WSMessage
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.runtime import GQLClientOptions
from iron_gql.testing.server import live_asgi_server
from iron_gql.testing.ws import SUBPROTOCOL
from iron_gql.testing.ws import accept_graphql_ws
from tests.conftest import accept_graphql_sse
from tests.conftest import make_sse_subscription_app
from tests.conftest import make_subscription_app
from tests.conftest import run_async

type _Mode = Literal["async", "sync"]
type _Operation = Literal["query", "graphql-sse", "graphql-transport-ws"]

_MODES: tuple[_Mode, ...] = ("async", "sync")
_HTTP_OPERATIONS: tuple[_Operation, ...] = ("query", "graphql-sse")
_SUBSCRIPTIONS: tuple[_Operation, ...] = ("graphql-sse", "graphql-transport-ws")
_OPERATIONS: tuple[_Operation, ...] = ("query", *_SUBSCRIPTIONS)

_every_mode = pytest.mark.parametrize("mode", _MODES)
_every_operation = pytest.mark.parametrize("operation", _OPERATIONS)
_http_operations = pytest.mark.parametrize("operation", _HTTP_OPERATIONS)

# Long enough for any healthy step, short enough to fail before pytest does.
_STEP_TIMEOUT = 10

_IN_PROCESS = "http://testserver/graphql"

# Every error has a text.
_ANY_TEXT = re.compile(r".", re.DOTALL)

# What a test runs in the body of a subscription's `with`.
type _Body = Callable[[], None]


class _Result(pydantic.BaseModel):
    counter: int


def _read_nothing() -> None:
    pass


async def _async_call(
    operation: _Operation,
    url: str,
    options: GQLClientOptions,
    body: _Body = _read_nothing,
    target_app: ASGIApp | None = None,
    model_base: type[pydantic.BaseModel] | None = None,
) -> list[int]:
    client = AsyncGQLClient(
        base_url=url,
        subscription_protocol=("graphql-sse" if operation == "query" else operation),
        target_app=target_app,
        model_base=model_base,
        **options,
    )
    try:
        if operation == "query":
            result = await client.query(
                _Result, "query { counter }", variables={}, headers={}
            )
            return [result.counter]
        async with client.subscribe(
            _Result, "subscription { counter }", variables={}, headers={}
        ) as stream:
            body()
            return [item.counter async for item in stream]
    finally:
        await client.close()


def _sync_call(
    operation: _Operation,
    url: str,
    options: GQLClientOptions,
    body: _Body = _read_nothing,
    model_base: type[pydantic.BaseModel] | None = None,
) -> list[int]:
    client = GQLClient(
        base_url=url,
        subscription_protocol=("graphql-sse" if operation == "query" else operation),
        model_base=model_base,
        **options,
    )
    try:
        if operation == "query":
            result = client.query(
                _Result, "query { counter }", variables={}, headers={}
            )
            return [result.counter]
        with client.subscribe(
            _Result, "subscription { counter }", variables={}, headers={}
        ) as stream:
            body()
            return [item.counter for item in stream]
    finally:
        client.close()


def _call(
    mode: _Mode,
    operation: _Operation,
    url: str,
    options: GQLClientOptions | None = None,
    body: _Body = _read_nothing,
    model_base: type[pydantic.BaseModel] | None = None,
) -> list[int]:
    match mode:
        case "async":
            return run_async(
                _async_call(operation, url, options or {}, body, model_base=model_base)
            )
        case "sync":
            return _sync_call(operation, url, options or {}, body, model_base)


def _failure(
    mode: _Mode,
    operation: _Operation,
    url: str,
    options: GQLClientOptions | None = None,
    body: _Body = _read_nothing,
    model_base: type[pydantic.BaseModel] | None = None,
) -> Exception:
    with pytest.raises(Exception, match=_ANY_TEXT) as failure:
        _call(mode, operation, url, options, body, model_base)
    return failure.value


def _in_process_failure(
    app: ASGIApp, operation: _Operation, body: _Body = _read_nothing
) -> Exception:
    with pytest.raises(Exception, match=_ANY_TEXT) as failure:
        run_async(_async_call(operation, _IN_PROCESS, {}, body, target_app=app))
    return failure.value


_SOCKET_ADDRESS = pydantic.TypeAdapter(tuple[str, int])


# A server that talks raw bytes: connections one after another, each in the
# hands of `talk` until it returns.
@contextmanager
def _tcp_server(talk: Callable[[socket.socket], None]) -> Iterator[str]:
    stopped = threading.Event()
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(0.05)
        _host, port = _SOCKET_ADDRESS.validate_python(listener.getsockname())

        def serve() -> None:
            while not stopped.is_set():
                try:
                    connection = listener.accept()[0]
                except TimeoutError:
                    continue
                connection.settimeout(_STEP_TIMEOUT)
                with connection, contextlib.suppress(ConnectionError):
                    talk(connection)

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}/graphql"
        finally:
            stopped.set()
            thread.join(_STEP_TIMEOUT)


_CONTENT_LENGTH = re.compile(rb"content-length: (\d+)", re.IGNORECASE)


def _read_request(connection: socket.socket) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        data += connection.recv(65536)
    head, _, body = data.partition(b"\r\n\r\n")
    match = _CONTENT_LENGTH.search(head)
    length = int(match.group(1)) if match else 0
    while len(body) < length:
        body += connection.recv(65536)


def _wait_for_hangup(connection: socket.socket) -> None:
    while connection.recv(65536):
        pass


def _reset(connection: socket.socket) -> None:
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    connection.close()


def _answering(response: bytes) -> Callable[[socket.socket], None]:
    def talk(connection: socket.socket) -> None:
        _read_request(connection)
        connection.sendall(response)

    return talk


def _response(
    status: int,
    body: bytes,
    *headers: str,
    content_type: str = "application/graphql-response+json",
    missing: int = 0,
) -> bytes:
    lines = [
        f"HTTP/1.1 {status} X",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body) + missing}",
        *headers,
    ]
    return "\r\n".join(lines).encode() + b"\r\n\r\n" + body


# The server's end of one websocket over a raw socket: unlike an ASGI app, it
# can send a close without a code, a close right behind a message, or nothing.
class _Peer:
    def __init__(self, connection: socket.socket):
        self.connection = connection
        self._ws = wsproto.WSConnection(wsproto.ConnectionType.SERVER)

    def event(self) -> wsproto.events.Event:
        while True:
            for event in self._ws.events():
                return event
            self._ws.receive_data(self.connection.recv(65536))

    def accept(self) -> None:
        assert isinstance(self.event(), wsproto.events.Request)
        self.send(wsproto.events.AcceptConnection(subprotocol=SUBPROTOCOL))

    def expect(self, message_type: str) -> None:
        event = self.event()
        assert isinstance(event, wsproto.events.TextMessage), event
        assert json.loads(event.data)["type"] == message_type

    # All in one write: the client finds the last of them behind the first.
    def send(self, *events: wsproto.events.Event, close: bytes = b"") -> None:
        frames = [self._ws.send(event) for event in events]
        self.connection.sendall(b"".join(frames) + close)

    def acknowledge(self) -> None:
        self.accept()
        self.expect("connection_init")
        self.send(_message({"type": "connection_ack"}))
        self.expect("subscribe")


def _message(message: dict[str, object]) -> wsproto.events.Event:
    return wsproto.events.TextMessage(data=json.dumps(message))


# A close frame as it goes over the wire: wsproto sends neither one without a
# code, which a client reads as code 1005, nor one with a code above 4999.
def _close(code: int, reason: str = "") -> bytes:
    no_code = code == CloseReason.NO_STATUS_RCVD
    payload = b"" if no_code else struct.pack("!H", code) + reason.encode()
    return struct.pack("!BB", 0x88, len(payload)) + payload


# --- the types -----------------------------------------------------------


def test_error_types_form_the_tree_of_the_model():
    parents: dict[type[Exception], type[Exception]] = {
        GraphQLClientError: Exception,
        TransportError: GraphQLClientError,
        SubscriptionInterruptedError: TransportError,
        ResponseError: GraphQLClientError,
        GraphQLResponseError: ResponseError,
        HTTPResponseError: ResponseError,
        ResponseProtocolError: ResponseError,
        WebSocketClosedError: ResponseError,
    }
    for error, parent in parents.items():
        assert error.__bases__ == (parent,)
        assert getattr(errors, error.__name__) is error
        assert getattr(iron_gql, error.__name__) is error
        assert error.__name__ in iron_gql.__all__


# GraphQL does not say that a `message` is not empty.
@_every_mode
def test_an_error_with_an_empty_message_still_has_a_text(mode: _Mode):
    body = b'{"errors": [{"message": "", "path": ["counter"]}]}'
    with _tcp_server(_answering(_response(200, body))) as url:
        error = _failure(mode, "query", url)
    assert isinstance(error, GraphQLResponseError)
    assert error.errors == [{"message": "", "path": ["counter"]}]


# --- the connection --------------------------------------------------------


def _assert_transport_error(error: Exception, cause: type[Exception]) -> None:
    assert type(error) is TransportError
    assert isinstance(error.__cause__, cause)


@_every_mode
@_every_operation
def test_a_refused_connection_is_a_transport_error(mode: _Mode, operation: _Operation):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        _host, port = _SOCKET_ADDRESS.validate_python(listener.getsockname())
    error = _failure(mode, operation, f"http://127.0.0.1:{port}/graphql")
    _assert_transport_error(error, httpx2.ConnectError)


@_every_mode
@_every_operation
@pytest.mark.parametrize("answer", [b"", b"HTTP/1.1 200", b"not http at all\r\n\r\n"])
def test_a_connection_broken_before_the_response_head_is_a_transport_error(
    mode: _Mode, operation: _Operation, answer: bytes
):
    with _tcp_server(_answering(answer)) as url:
        error = _failure(mode, operation, url)
    _assert_transport_error(error, httpx2.RemoteProtocolError)


@_every_mode
@_every_operation
def test_a_connection_reset_before_the_response_head_is_a_transport_error(
    mode: _Mode, operation: _Operation
):
    def talk(connection: socket.socket) -> None:
        _read_request(connection)
        _reset(connection)

    with _tcp_server(talk) as url:
        error = _failure(mode, operation, url)
    _assert_transport_error(error, httpx2.ReadError)


# The async client opens a WebSocket without its `query_timeout`, under the
# default of `httpx2`; a hook makes that shorter.
def _read_timeout_hook(mode: _Mode) -> Callable[..., object]:
    def hook(request: httpx2.Request) -> None:
        request.extensions["timeout"] = httpx2.Timeout(1, read=0.2).as_dict()

    async def async_hook(request: httpx2.Request) -> None:
        await asyncio.sleep(0)
        hook(request)

    return async_hook if mode == "async" else hook


@_every_mode
@_every_operation
def test_waiting_too_long_for_the_response_head_is_a_transport_error(
    mode: _Mode, operation: _Operation
):
    options = GQLClientOptions(
        httpx_options={"event_hooks": {"request": [_read_timeout_hook(mode)]}}
    )
    with _tcp_server(_wait_for_hangup) as url:
        error = _failure(mode, operation, url, options)
    _assert_transport_error(error, httpx2.ReadTimeout)


@_every_mode
@_every_operation
def test_a_proxy_that_refuses_is_a_transport_error(mode: _Mode, operation: _Operation):
    # An https URL makes the client ask the proxy for a tunnel.
    refusal = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
    with _tcp_server(_answering(refusal)) as proxy:
        error = _failure(
            mode,
            operation,
            "https://testserver.invalid/graphql",
            {"httpx_options": {"proxy": proxy.removesuffix("/graphql")}},
        )
    _assert_transport_error(error, httpx2.ProxyError)


@_every_mode
@_http_operations
def test_a_2xx_body_cut_short_is_a_transport_error(mode: _Mode, operation: _Operation):
    response = _response(200, b'{"data":', missing=10)
    with _tcp_server(_answering(response)) as url:
        error = _failure(mode, operation, url)
    _assert_transport_error(error, httpx2.RemoteProtocolError)


# --- what was asked cannot be sent ---------------------------------------------


@_every_mode
@_http_operations
def test_a_scheme_that_is_not_http_is_a_wrong_call(mode: _Mode, operation: _Operation):
    error = _failure(mode, operation, "ftp://testserver/graphql")
    assert type(error) is ValueError
    assert "base_url" in str(error)
    assert isinstance(error.__cause__, httpx2.UnsupportedProtocol)


@_every_mode
@_every_operation
def test_a_header_http_does_not_allow_is_a_wrong_call(
    mode: _Mode, operation: _Operation
):
    with _tcp_server(_wait_for_hangup) as url:
        error = _failure(mode, operation, url, {"headers": {"X-Bad": "one\ntwo"}})
    assert type(error) is ValueError
    assert "headers" in str(error)
    assert isinstance(error.__cause__, httpx2.LocalProtocolError)


# --- the response ------------------------------------------------------------


_REFUSAL = b'{"errors": [{"message": "unavailable"}]}'


@_every_mode
@_http_operations
def test_a_503_and_a_401_with_the_same_errors_differ_by_their_status(
    mode: _Mode, operation: _Operation
):
    statuses: list[int | None] = []
    for status in (503, 401):
        with _tcp_server(_answering(_response(status, _REFUSAL))) as url:
            error = _failure(mode, operation, url)
        assert isinstance(error, GraphQLResponseError)
        assert error.errors == [{"message": "unavailable"}]
        statuses.append(error.status_code)
    assert statuses == [503, 401]


# Whatever its type and its errors, a refusal whose body did not arrive is its
# status and nothing more.
@_every_mode
@_http_operations
@pytest.mark.parametrize("status", [302, 401, 503])
def test_a_refusal_with_a_body_cut_short_is_its_status(
    mode: _Mode, operation: _Operation, status: int
):
    response = _response(status, _REFUSAL, "Location: /elsewhere", missing=10)
    with _tcp_server(_answering(response)) as url:
        error = _failure(mode, operation, url)
    assert isinstance(error, HTTPResponseError)
    assert error.status_code == error.response.status_code == status
    assert str(status) in str(error)
    assert isinstance(error.__cause__, httpx2.RemoteProtocolError)


# `httpx2` parses the `Location` whether it follows it or not, and gives up the
# response when it cannot.
@_every_mode
@_every_operation
def test_a_redirect_to_no_url_is_a_transport_error(mode: _Mode, operation: _Operation):
    response = _response(302, b"", "Location: http://testserver:port/")
    with _tcp_server(_answering(response)) as url:
        error = _failure(mode, operation, url)
    _assert_transport_error(error, httpx2.RemoteProtocolError)


# `httpx2` follows five statuses, and only with a `Location`.
@_every_mode
@_every_operation
@pytest.mark.parametrize("follow_redirects", [False, True])
@pytest.mark.parametrize(
    ("status", "headers"),
    [(302, []), (304, ["Location: http://testserver:port/"])],
    ids=["no-location", "not-a-redirect"],
)
def test_a_3xx_that_httpx2_never_follows_is_its_status(
    mode: _Mode,
    operation: _Operation,
    follow_redirects: bool,
    status: int,
    headers: list[str],
):
    options = GQLClientOptions(httpx_options={"follow_redirects": follow_redirects})
    with _tcp_server(_answering(_response(status, b"", *headers))) as url:
        error = _failure(mode, operation, url, options)
    assert isinstance(error, HTTPResponseError)
    assert error.status_code == status
    for header in headers:
        assert header.removeprefix("Location: ") in str(error)


@_every_mode
@_http_operations
def test_a_refusal_with_a_body_that_does_not_decode_is_its_status(
    mode: _Mode, operation: _Operation
):
    response = _response(503, b"not gzip", "Content-Encoding: gzip")
    with _tcp_server(_answering(response)) as url:
        error = _failure(mode, operation, url)
    assert isinstance(error, HTTPResponseError)
    assert error.status_code == 503
    assert isinstance(error.__cause__, httpx2.DecodingError)


@_every_mode
@_http_operations
def test_a_2xx_body_that_does_not_decode_is_a_protocol_error(
    mode: _Mode, operation: _Operation
):
    response = _response(200, b"not gzip", "Content-Encoding: gzip")
    with _tcp_server(_answering(response)) as url:
        error = _failure(mode, operation, url)
    assert isinstance(error, ResponseProtocolError)
    assert error.status_code == 200
    assert isinstance(error.__cause__, httpx2.DecodingError)


@_every_mode
@_http_operations
def test_a_status_of_no_class_is_an_http_response_error(
    mode: _Mode, operation: _Operation
):
    with _tcp_server(_answering(_response(799, _REFUSAL))) as url:
        error = _failure(mode, operation, url)
    assert isinstance(error, HTTPResponseError)
    assert error.status_code == 799
    assert error.response.content == _REFUSAL


@_every_mode
@_every_operation
def test_too_many_redirects_are_a_protocol_error(mode: _Mode, operation: _Operation):
    response = _response(307, b"", "Location: /graphql", "Connection: close")
    options = GQLClientOptions(
        httpx_options={"follow_redirects": True, "max_redirects": 2}
    )
    with _tcp_server(_answering(response)) as url:
        error = _failure(mode, operation, url, options)
    assert isinstance(error, ResponseProtocolError)
    assert error.status_code is None
    assert isinstance(error.__cause__, httpx2.TooManyRedirects)


@_every_mode
@_http_operations
@pytest.mark.parametrize("body", [b"[1, 2]", b"not json"])
def test_a_malformed_2xx_body_keeps_its_diagnosis_as_the_cause(
    mode: _Mode, operation: _Operation, body: bytes
):
    with _tcp_server(_answering(_response(200, body))) as url:
        error = _failure(mode, operation, url)
    assert isinstance(error, ResponseProtocolError)
    assert isinstance(error.__cause__, pydantic.ValidationError)


# --- the event stream -------------------------------------------------------------


def _event_stream(body: bytes, *headers: str, missing: int = 0) -> bytes:
    return _response(
        200, body, *headers, content_type="text/event-stream", missing=missing
    )


_NEXT = b'event: next\ndata: {"data": {"counter": 1}}\n\n'


@_every_mode
@pytest.mark.parametrize("events", [b"", _NEXT])
def test_a_stream_that_ends_without_complete_is_interrupted(mode: _Mode, events: bytes):
    with _tcp_server(_answering(_event_stream(events))) as url:
        error = _failure(mode, "graphql-sse", url)
    assert isinstance(error, SubscriptionInterruptedError)
    assert error.__cause__ is None


@_every_mode
@pytest.mark.parametrize("events", [b"", _NEXT])
def test_a_stream_cut_short_is_interrupted(mode: _Mode, events: bytes):
    with _tcp_server(_answering(_event_stream(events, missing=10))) as url:
        error = _failure(mode, "graphql-sse", url)
    assert isinstance(error, SubscriptionInterruptedError)
    assert isinstance(error.__cause__, httpx2.RemoteProtocolError)


@_every_mode
@pytest.mark.parametrize("events", [b"", _NEXT])
def test_a_stream_reset_is_interrupted(mode: _Mode, events: bytes):
    def talk(connection: socket.socket) -> None:
        _read_request(connection)
        connection.sendall(_event_stream(events, missing=10))
        # The head has to be read before the reset drops what was not.
        time.sleep(0.2)
        _reset(connection)

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-sse", url)
    assert isinstance(error, SubscriptionInterruptedError)
    assert isinstance(error.__cause__, httpx2.ReadError)


@_every_mode
@pytest.mark.parametrize("events", [b"", _NEXT])
def test_a_stream_silent_past_the_idle_timeout_is_interrupted(
    mode: _Mode, events: bytes
):
    def talk(connection: socket.socket) -> None:
        _read_request(connection)
        connection.sendall(_event_stream(events, missing=10))
        _wait_for_hangup(connection)

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-sse", url, {"subscription_idle_timeout": 0.2})
    assert isinstance(error, SubscriptionInterruptedError)
    assert isinstance(error.__cause__, httpx2.ReadTimeout)


@_every_mode
def test_a_stream_that_does_not_decode_is_a_protocol_error(mode: _Mode):
    stream = _event_stream(b"not gzip", "Content-Encoding: gzip")
    with _tcp_server(_answering(stream)) as url:
        error = _failure(mode, "graphql-sse", url)
    assert isinstance(error, ResponseProtocolError)
    assert error.status_code == 200
    assert isinstance(error.__cause__, httpx2.DecodingError)


@_every_mode
def test_an_event_over_the_size_limit_is_a_protocol_error(mode: _Mode):
    event = b"event: next\ndata: " + b"x" * (1024 * 1024 + 1) + b"\n\n"
    stream = _event_stream(gzip.compress(event), "Content-Encoding: gzip")
    with _tcp_server(_answering(stream)) as url:
        error = _failure(mode, "graphql-sse", url)
    assert isinstance(error, ResponseProtocolError)
    assert error.status_code == 200
    assert isinstance(error.__cause__, httpx2.SSEError)


# The stream was opened with a status of its own choice among 2xx.
@_every_mode
@pytest.mark.parametrize(
    ("event", "cause"),
    [
        (b'event: next\ndata: {"data": null}\n\n', None),
        (b'event: next\ndata: {"data": null, "errors": []}\n\n', None),
        (b'event: next\ndata: {"errors": null}\n\n', None),
        (b"event: next\ndata: []\n\n", pydantic.ValidationError),
        (b"event: next\ndata: {not json\n\n", pydantic.ValidationError),
        (b'data: {"data": {"counter": 1}}\n\n', None),
        (b"event: error\ndata: {}\n\n", None),
    ],
)
def test_an_event_that_breaks_the_protocol_is_a_protocol_error(
    mode: _Mode, event: bytes, cause: type[Exception] | None
):
    stream = _response(294, event, content_type="text/event-stream")
    with _tcp_server(_answering(stream)) as url:
        error = _failure(mode, "graphql-sse", url)
    assert isinstance(error, ResponseProtocolError)
    assert error.status_code == 294
    assert isinstance(error.__cause__, cause or type(None))


@_every_mode
def test_a_next_with_errors_has_the_status_of_its_stream(mode: _Mode):
    event = b'event: next\ndata: {"data": {"counter": 1}, "errors": [%s]}\n\n'
    stream = _response(
        294, event % b'{"message": "revoked"}', content_type="text/event-stream"
    )
    with _tcp_server(_answering(stream)) as url:
        error = _failure(mode, "graphql-sse", url)
    assert isinstance(error, GraphQLResponseError)
    assert error.errors == [{"message": "revoked"}]
    assert error.status_code == 294


@_every_mode
@_every_operation
@pytest.mark.parametrize("no_errors", [[], None])
def test_empty_or_null_errors_beside_data_are_no_errors(
    mode: _Mode, operation: _Operation, no_errors: list[object] | None
):
    payload: dict[str, object] = {"data": {"counter": 1}, "errors": no_errors}
    if operation == "query":
        with _tcp_server(
            _answering(_response(200, json.dumps(payload).encode()))
        ) as url:
            assert _call(mode, operation, url) == [1]
        return
    app = (
        make_sse_subscription_app([payload])
        if operation == "graphql-sse"
        else make_subscription_app([
            {"type": "next", "payload": payload},
            {"type": "complete"},
        ])
    )
    with live_asgi_server(app) as url:
        assert _call(mode, operation, url) == [1]


@_every_mode
@pytest.mark.parametrize("operation", _SUBSCRIPTIONS)
def test_data_that_does_not_fit_the_model_is_a_validation_error(
    mode: _Mode, operation: _Operation
):
    payload: dict[str, object] = {"data": {"counter": "many"}}
    app = (
        make_sse_subscription_app([payload])
        if operation == "graphql-sse"
        else make_subscription_app([{"type": "next", "payload": payload}])
    )
    with live_asgi_server(app) as url:
        error = _failure(mode, operation, url)
    assert isinstance(error, pydantic.ValidationError)


# --- opening a websocket -------------------------------------------------------


@_every_mode
@pytest.mark.parametrize("status", [200, 302, 400, 403, 503])
def test_a_refused_upgrade_is_an_http_response_error(mode: _Mode, status: int):
    response = _response(status, _REFUSAL, "Location: /elsewhere")
    with _tcp_server(_answering(response)) as url:
        error = _failure(mode, "graphql-transport-ws", url)
    assert isinstance(error, HTTPResponseError)
    assert error.status_code == error.response.status_code == status
    assert error.response.headers["Location"] == "/elsewhere"
    assert str(status) in str(error)
    assert ("/elsewhere" in str(error)) is (status == 302)
    assert isinstance(error.__cause__, WebSocketUpgradeError)


def test_an_upgrade_refused_by_a_target_app_is_an_http_response_error():
    async def app(_scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        await receive()
        await send({
            "type": "websocket.http.response.start",
            "status": 403,
            "headers": [(b"x-reason", b"revoked")],
        })
        await send({"type": "websocket.http.response.body", "body": _REFUSAL})

    error = _in_process_failure(app, "graphql-transport-ws")
    assert isinstance(error, HTTPResponseError)
    assert error.status_code == 403
    assert error.response.headers["x-reason"] == "revoked"
    with pytest.raises(RuntimeError, match="request"):
        _ = error.response.request
    assert isinstance(error.__cause__, WebSocketUpgradeError)


def test_a_target_app_that_does_not_answer_the_connection_is_a_runtime_error():
    async def app(_scope: ASGIScope, _receive: ASGIReceive, _send: ASGISend) -> None:
        await asyncio.sleep(_STEP_TIMEOUT)

    error = _in_process_failure(app, "graphql-transport-ws")
    assert type(error) is RuntimeError


@pytest.mark.parametrize("mode", _MODES)
def test_silence_before_connection_ack_is_a_transport_error(
    mode: _Mode, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(websockets, "_WS_CONNECTION_ACK_TIMEOUT_SECONDS", 0.1)

    def talk(connection: socket.socket) -> None:
        _Peer(connection).accept()
        _wait_for_hangup(connection)

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-transport-ws", url)
    _assert_transport_error(error, TimeoutError)


# --- the end of a websocket ------------------------------------------------------


type _Moment = Literal["before-ack", "after-subscribe"]
_MOMENTS: tuple[_Moment, ...] = ("before-ack", "after-subscribe")


# How a connection ends: a code the server closes with, or no close at all.
type _End = int | Literal["no-close"]


# What the model says of each end, by whether `subscribe` has gone out.
def _judge_end(error: Exception, end: _End, *, subscribed: bool, reason: str) -> None:
    broken = SubscriptionInterruptedError if subscribed else TransportError
    match end:
        case 4403:
            assert type(error) is WebSocketClosedError
            assert error.close_code == end
            assert error.reason == reason
            assert error.status_code is None
        case 1009:
            assert type(error) is ResponseProtocolError
            assert error.status_code is None
        case _:
            assert type(error) is broken
    if end == "no-close":
        assert isinstance(error.__cause__, WebSocketNetworkError)
    else:
        assert isinstance(error.__cause__, WebSocketDisconnect)
        assert str(end) in str(error)
        assert reason in str(error)


_ENDS: tuple[_End, ...] = (
    "no-close",
    1000,
    1001,
    CloseReason.NO_STATUS_RCVD.value,
    1009,
    1011,
    4403,
)


def _end_with(peer: _Peer, end: _End, reason: str) -> None:
    if end != "no-close":
        peer.send(close=_close(end, reason))
        _wait_for_hangup(peer.connection)


@_every_mode
@pytest.mark.parametrize("moment", _MOMENTS)
@pytest.mark.parametrize("end", _ENDS)
def test_the_end_of_a_connection_is_judged_by_its_code(
    mode: _Mode, moment: _Moment, end: _End
):
    # A close without a code cannot carry a reason either.
    reason = "gone" if end in {1011, 4403} else ""

    def talk(connection: socket.socket) -> None:
        peer = _Peer(connection)
        if moment == "before-ack":
            peer.accept()
            peer.expect("connection_init")
        else:
            peer.acknowledge()
        _end_with(peer, end, reason)

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-transport-ws", url)
    _judge_end(error, end, subscribed=moment == "after-subscribe", reason=reason)


# The bounds of the codes that refuse, and a refusal without a reason.
@_every_mode
@pytest.mark.parametrize("code", [3999, 4000, 4999, 5000])
def test_the_codes_from_4000_to_4999_refuse(mode: _Mode, code: int):
    def talk(connection: socket.socket) -> None:
        peer = _Peer(connection)
        peer.acknowledge()
        peer.send(close=_close(code))
        _wait_for_hangup(connection)

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-transport-ws", url)
    if code in {4000, 4999}:
        assert isinstance(error, WebSocketClosedError)
        assert (error.close_code, error.reason) == (code, "")
    else:
        assert type(error) is SubscriptionInterruptedError


@pytest.mark.parametrize("moment", ["before-accept", *_MOMENTS])
@pytest.mark.parametrize("code", [1000, 1009, 1011, 4403])
def test_a_close_by_a_target_app_is_judged_by_its_code(moment: str, code: int):
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if moment == "before-accept":
            await receive()
            await send({"type": "websocket.close", "code": code, "reason": "gone"})
            return
        connection = await accept_graphql_ws(scope, receive, send)
        if moment == "after-subscribe":
            await connection.ack()
        await connection.close(code, "gone")

    error = _in_process_failure(app, "graphql-transport-ws")
    _judge_end(error, code, subscribed=moment == "after-subscribe", reason="gone")


@_every_mode
def test_a_message_over_the_size_limit_is_a_protocol_error(mode: _Mode):
    def talk(connection: socket.socket) -> None:
        peer = _Peer(connection)
        peer.acknowledge()
        peer.send(_message({"type": "next", "payload": "x" * 100_000}))
        _wait_for_hangup(connection)

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-transport-ws", url)
    _judge_end(error, 1009, subscribed=True, reason="too big")


# Holds the client on a `connection_ack` or a `ping` until what the server sent
# right behind it has arrived.
class _SlowReader(pydantic.BaseModel):
    @override
    def model_post_init(self, context: object, /) -> None:
        if isinstance(self, WSMessage) and self.type in {"connection_ack", "ping"}:
            time.sleep(0.2)


@_every_mode
@pytest.mark.parametrize("end", _ENDS)
def test_an_end_found_when_subscribing_is_judged_by_its_code(mode: _Mode, end: _End):
    # wsproto refuses to send `subscribe` on a connection that is closing: the
    # close that says why is still to be read. The sync session reads ahead of
    # the client, so there another message may come before it; the async one
    # stops at a message until the client takes it.
    reason = "gone" if end in {1011, 4403} else ""

    # What the client sent after the server's last word.
    arrived = b""
    last_word_sent = threading.Event()

    class _Reader(_SlowReader):
        @override
        def model_post_init(self, context: object, /) -> None:
            if isinstance(self, WSMessage) and self.type == "connection_ack":
                last_word_sent.wait()
            super().model_post_init(context)

    def talk(connection: socket.socket) -> None:
        nonlocal arrived
        peer = _Peer(connection)
        peer.accept()
        peer.expect("connection_init")
        sent = [_message({"type": "connection_ack"})]
        if mode == "sync":
            sent.append(_message({"type": "pong"}))
        if end == "no-close":
            peer.send(*sent)
            connection.shutdown(socket.SHUT_WR)
        else:
            peer.send(*sent, close=_close(end, reason))
        last_word_sent.set()
        while chunk := connection.recv(65536):
            arrived += chunk

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-transport-ws", url, model_base=_Reader)
    # A text frame opens with 0x81, a close frame with 0x88. A socket that is
    # gone without a close frame takes `subscribe` until the client learns of
    # it: what the server received says which error is due.
    subscribed = arrived.startswith(b"\x81")
    assert end == "no-close" or not subscribed
    _judge_end(error, end, subscribed=subscribed, reason=reason)


@_every_mode
def test_messages_before_a_close_are_read_though_a_ping_goes_unanswered(mode: _Mode):
    def talk(connection: socket.socket) -> None:
        peer = _Peer(connection)
        peer.acknowledge()
        peer.send(
            _message({"type": "ping"}),
            _message({"type": "next", "payload": {"data": {"counter": 1}}}),
            _message({"type": "complete"}),
            close=_close(1000),
        )
        _wait_for_hangup(connection)

    with _tcp_server(talk) as url:
        assert _call(mode, "graphql-transport-ws", url, model_base=_SlowReader) == [1]


# What breaks under a write is the socket; the reading side sees a break first,
# so the test breaks the write itself.
@_every_mode
@pytest.mark.parametrize(
    ("message_type", "expected"),
    [
        ("connection_init", TransportError),
        ("subscribe", TransportError),
        ("pong", SubscriptionInterruptedError),
    ],
)
def test_a_write_that_fails_is_a_broken_connection(
    mode: _Mode,
    message_type: str,
    expected: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
):
    sync_send = WebSocketSession.send_json
    async_send = AsyncWebSocketSession.send_json

    def send(session: WebSocketSession, message: dict[str, object]) -> None:
        if message["type"] == message_type:
            raise WebSocketNetworkError
        sync_send(session, message)

    async def send_async(
        session: AsyncWebSocketSession, message: dict[str, object]
    ) -> None:
        if message["type"] == message_type:
            raise WebSocketNetworkError
        await async_send(session, message)

    monkeypatch.setattr(WebSocketSession, "send_json", send)
    monkeypatch.setattr(AsyncWebSocketSession, "send_json", send_async)

    def talk(connection: socket.socket) -> None:
        peer = _Peer(connection)
        peer.accept()
        if message_type != "connection_init":
            peer.expect("connection_init")
            peer.send(_message({"type": "connection_ack"}))
        if message_type == "pong":
            peer.expect("subscribe")
            peer.send(_message({"type": "ping"}))
        _wait_for_hangup(connection)

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-transport-ws", url)
    assert type(error) is expected
    assert isinstance(error.__cause__, WebSocketNetworkError)


# --- messages that break graphql-transport-ws ----------------------------------


_BAD_MESSAGES: dict[str, tuple[wsproto.events.Event, type[Exception] | None]] = {
    "binary": (
        wsproto.events.BytesMessage(data=b"{}"),
        WebSocketInvalidTypeReceived,
    ),
    "not-json": (
        wsproto.events.TextMessage(data="{not json"),
        pydantic.ValidationError,
    ),
    "not-an-object": (
        wsproto.events.TextMessage(data="[]"),
        pydantic.ValidationError,
    ),
    "unknown-type": (_message({"type": "greeting"}), None),
    "no-type": (_message({}), None),
}

_BAD_STREAM_MESSAGES: dict[str, tuple[wsproto.events.Event, type[Exception] | None]] = {
    "next-no-payload": (_message({"type": "next"}), None),
    "next-no-data": (_message({"type": "next", "payload": {"errors": []}}), None),
    "next-null-data": (_message({"type": "next", "payload": {"data": None}}), None),
    "next-malformed": (
        _message({"type": "next", "payload": {"data": []}}),
        pydantic.ValidationError,
    ),
    "error-no-payload": (_message({"type": "error"}), None),
    "error-empty": (_message({"type": "error", "payload": []}), None),
    "error-malformed": (
        _message({"type": "error", "payload": {"message": "x"}}),
        pydantic.ValidationError,
    ),
    "second-ack": (_message({"type": "connection_ack"}), None),
}


@_every_mode
@pytest.mark.parametrize(
    ("moment", "name"),
    [
        *(("before-ack", name) for name in _BAD_MESSAGES),
        *(("after-subscribe", name) for name in _BAD_MESSAGES),
        *(("after-subscribe", name) for name in _BAD_STREAM_MESSAGES),
        ("before-ack", "next-no-data"),
    ],
)
def test_a_message_that_breaks_the_protocol_is_a_protocol_error(
    mode: _Mode, moment: _Moment, name: str
):
    message, cause = {**_BAD_MESSAGES, **_BAD_STREAM_MESSAGES}[name]

    def talk(connection: socket.socket) -> None:
        peer = _Peer(connection)
        if moment == "before-ack":
            peer.accept()
            peer.expect("connection_init")
        else:
            peer.acknowledge()
        peer.send(message)
        _wait_for_hangup(connection)

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-transport-ws", url)
    assert type(error) is ResponseProtocolError
    assert error.status_code is None
    assert isinstance(error.__cause__, cause or type(None))


@_every_mode
def test_the_limit_of_messages_before_connection_ack_is_named_as_the_clients(
    mode: _Mode,
):
    def talk(connection: socket.socket) -> None:
        peer = _Peer(connection)
        peer.accept()
        peer.expect("connection_init")
        peer.send(*[_message({"type": "pong"})] * 16)
        _wait_for_hangup(connection)

    with _tcp_server(talk) as url:
        error = _failure(mode, "graphql-transport-ws", url)
    assert type(error) is ResponseProtocolError
    assert "16 messages, the limit of the client" in str(error)


@_every_mode
@pytest.mark.parametrize(
    "message",
    [
        {"type": "next", "payload": {"data": None, "errors": [{"message": "no"}]}},
        {
            "type": "next",
            "payload": {"data": {"counter": 1}, "errors": [{"message": "no"}]},
        },
        {"type": "error", "payload": [{"message": "no"}]},
    ],
)
def test_errors_over_a_websocket_have_no_status(
    mode: _Mode, message: dict[str, object]
):
    with live_asgi_server(make_subscription_app([message])) as url:
        error = _failure(mode, "graphql-transport-ws", url)
    assert type(error) is GraphQLResponseError
    assert error.errors == [{"message": "no"}]
    assert error.status_code is None


# --- what is not a failure of the operation ------------------------------------


def _idle_app(operation: _Operation) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if operation == "graphql-sse":
            request = await accept_graphql_sse(scope, receive, send)
            await request.open_stream()
            await request.wait_disconnect()
        else:
            connection = await accept_graphql_ws(scope, receive, send)
            await connection.ack()
            await connection.drain()

    return app


def _of_the_caller() -> dict[str, Exception]:
    with_cause = KeyError("caller")
    with_cause.__cause__ = ZeroDivisionError("cause")
    return {
        "read-error": httpx2.ReadError("caller"),
        "connect-error-group": ExceptionGroup(
            "caller", [httpx2.ConnectError("caller")]
        ),
        "group-of-a-group": ExceptionGroup(
            "caller", [ExceptionGroup("inner", [ValueError("caller")])]
        ),
        "disconnect": WebSocketDisconnect(4403, "caller"),
        "network-error": WebSocketNetworkError("caller"),
        "with-a-cause": with_cause,
    }


def _raising(error: BaseException) -> _Body:
    def body() -> None:
        raise error

    return body


def _assert_untouched(caught: BaseException, error: BaseException) -> None:
    assert caught is error
    assert error.__context__ is None
    if isinstance(error, KeyError):
        assert isinstance(error.__cause__, ZeroDivisionError)
    else:
        assert error.__cause__ is None


@_every_mode
@pytest.mark.parametrize("operation", _SUBSCRIPTIONS)
@pytest.mark.parametrize("name", list(_of_the_caller()))
def test_an_exception_of_the_callers_body_stays_itself(
    mode: _Mode, operation: _Operation, name: str
):
    error = _of_the_caller()[name]
    with (
        live_asgi_server(_idle_app(operation)) as url,
        pytest.raises(type(error)) as caught,
    ):
        _call(mode, operation, url, body=_raising(error))
    _assert_untouched(caught.value, error)


@pytest.mark.parametrize("name", list(_of_the_caller()))
def test_an_exception_of_the_callers_body_over_a_target_app_stays_itself(name: str):
    error = _of_the_caller()[name]
    with pytest.raises(type(error)) as caught:
        run_async(
            _async_call(
                "graphql-transport-ws",
                _IN_PROCESS,
                {},
                _raising(error),
                target_app=_idle_app("graphql-transport-ws"),
            )
        )
    _assert_untouched(caught.value, error)


@pytest.mark.parametrize("operation", _SUBSCRIPTIONS)
def test_a_cancelled_subscription_is_cancelled(operation: _Operation):
    async def run(url: str) -> None:
        opened = asyncio.Event()
        task = asyncio.create_task(_async_call(operation, url, {}, opened.set))
        await opened.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    with live_asgi_server(_idle_app(operation)) as url:
        run_async(run(url))


@_every_mode
@pytest.mark.parametrize("operation", _SUBSCRIPTIONS)
def test_generator_exit_in_the_callers_body_stays_itself(
    mode: _Mode, operation: _Operation
):
    error = GeneratorExit()
    with (
        live_asgi_server(_idle_app(operation)) as url,
        pytest.raises(GeneratorExit) as caught,
    ):
        _call(mode, operation, url, body=_raising(error))
    assert caught.value is error


class _Auth(httpx2.Auth):
    def __init__(self, error: Exception):
        self._error = error

    @override
    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response]:
        _raising(self._error)()
        yield request


def _request_hook(mode: _Mode, error: Exception) -> Callable[..., object]:
    def hook(_request: httpx2.Request) -> None:
        raise error

    async def async_hook(_request: httpx2.Request) -> None:
        await asyncio.sleep(0)
        raise error

    return async_hook if mode == "async" else hook


type _Place = Literal["auth", "event_hooks"]
_PLACES: tuple[_Place, ...] = ("auth", "event_hooks")


def _callback_options(mode: _Mode, place: _Place, error: Exception) -> GQLClientOptions:
    match place:
        case "auth":
            return {"httpx_options": {"auth": _Auth(error)}}
        case "event_hooks":
            hook = _request_hook(mode, error)
            return {"httpx_options": {"event_hooks": {"request": [hook]}}}


def _of_a_callback() -> dict[str, Exception]:
    request = httpx2.Request("GET", "http://auth.invalid")
    return {
        "custom": KeyError("caller"),
        "group": ExceptionGroup("caller", [ValueError("caller")]),
        "status": httpx2.HTTPStatusError(
            "caller", request=request, response=httpx2.Response(401, request=request)
        ),
        # Of the family of `httpx2`, but not in the table.
        "request-error": httpx2.RequestError("caller"),
    }


@_every_mode
@_every_operation
@pytest.mark.parametrize("place", _PLACES)
@pytest.mark.parametrize("name", list(_of_a_callback()))
def test_an_exception_of_a_callback_stays_itself(
    mode: _Mode, operation: _Operation, place: _Place, name: str
):
    error = _of_a_callback()[name]
    caught = _failure(
        mode,
        operation,
        "http://127.0.0.1:1/graphql",
        _callback_options(mode, place, error),
    )
    assert caught is error


@_every_mode
@_every_operation
@pytest.mark.parametrize("place", _PLACES)
def test_a_connection_failure_of_a_callback_is_a_transport_error(
    mode: _Mode, operation: _Operation, place: _Place
):
    error = httpx2.ConnectError("no token server")
    caught = _failure(
        mode,
        operation,
        "http://127.0.0.1:1/graphql",
        _callback_options(mode, place, error),
    )
    assert type(caught) is TransportError
    assert caught.__cause__ is error


def test_a_group_from_auth_over_a_target_app_stays_itself():
    error = ExceptionGroup("caller", [ValueError("caller")])
    with pytest.raises(ExceptionGroup) as caught:
        run_async(
            _async_call(
                "graphql-transport-ws",
                _IN_PROCESS,
                {"httpx_options": {"auth": _Auth(error)}},
                target_app=_idle_app("graphql-transport-ws"),
            )
        )
    assert caught.value is error


@pytest.mark.parametrize(
    "error", [KeyError("app"), httpx2.HTTPError("app")], ids=["custom", "unnamed"]
)
def test_an_exception_of_a_target_app_stays_itself(error: Exception):
    async def app(_scope: ASGIScope, receive: ASGIReceive, _send: ASGISend) -> None:
        await receive()
        raise error

    assert _in_process_failure(app, "query") is error


# What `httpx2` itself fails with while it reads for the client has no type of
# the model.
def test_an_unnamed_failure_of_a_session_stays_itself():
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        connection = await accept_graphql_ws(scope, receive, send)
        await connection.ack()
        await send({"type": "websocket.unheard-of"})
        await connection.drain()

    with pytest.raises(Exception, match="unheard-of") as failure:
        run_async(_async_call("graphql-transport-ws", _IN_PROCESS, {}, target_app=app))
    # Not a public name of `httpx2`.
    assert type(failure.value).__name__ == "UnhandledASGIMessageType"


@pytest.mark.parametrize(
    ("url", "headers"),
    [("ftp://testserver/graphql", {}), (_IN_PROCESS, {"X-Bad": "one\ntwo"})],
)
def test_a_target_app_gets_a_query_no_server_would(url: str, headers: dict[str, str]):
    got: ASGIScope = {}

    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        got.update(scope)
        await receive()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b'{"data": {"counter": 1}}'})

    options = GQLClientOptions(headers=headers)
    assert run_async(_async_call("query", url, options, target_app=app)) == [1]
    assert got["scheme"] == httpx2.URL(url).scheme
    sent = cast("list[tuple[bytes, bytes]]", got["headers"])
    assert dict(sent).get(b"x-bad", b"").decode() == headers.get("X-Bad", "")


# `httpx2` closes a WebSocket for the app that failed behind it.
def test_an_exception_of_a_target_app_behind_a_websocket_is_a_close():
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        await accept_graphql_ws(scope, receive, send)
        message = "app"
        raise KeyError(message)

    error = _in_process_failure(app, "graphql-transport-ws")
    _judge_end(error, 1011, subscribed=False, reason="app")


@pytest.mark.parametrize("left_out", ["code", "reason"])
def test_a_close_by_a_target_app_without_a_field_is_a_key_error(left_out: str):
    close: dict[str, object] = {
        "type": "websocket.close",
        "code": 4403,
        "reason": "gone",
    }
    del close[left_out]

    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        connection = await accept_graphql_ws(scope, receive, send)
        await connection.ack()
        await send(close)
        await connection.drain()

    error = _in_process_failure(app, "graphql-transport-ws")
    assert type(error) is KeyError
    assert error.args == (left_out,)


def test_a_connection_failure_of_a_target_app_is_a_transport_error():
    error = httpx2.ConnectError("no database")

    async def app(_scope: ASGIScope, receive: ASGIReceive, _send: ASGISend) -> None:
        await receive()
        raise error

    caught = _in_process_failure(app, "query")
    assert type(caught) is TransportError
    assert caught.__cause__ is error


# A setting `httpx2` refuses is not a failed operation, though an async client
# learns of it in one.
@_every_mode
@_every_operation
def test_a_setting_that_httpx2_refuses_stays_its_exception(
    mode: _Mode, operation: _Operation
):
    caught = _failure(
        mode,
        operation,
        "http://127.0.0.1:1/graphql",
        {"httpx_options": {"proxy": "gopher://proxy.invalid"}},
    )
    assert type(caught) is ValueError
    assert caught.__cause__ is None


# --- failures beside the caller's own -------------------------------------------


# A server that leaves a message the caller does not read -- which holds up the
# reading side of the session -- and is gone when the keepalive ping goes out:
# the only thing to notice is the ping's own write.
def _talk_then_vanish(vanish: threading.Event) -> Callable[[socket.socket], None]:
    def talk(connection: socket.socket) -> None:
        peer = _Peer(connection)
        peer.acknowledge()
        peer.send(_message({"type": "next", "payload": {"data": {"counter": 1}}}))
        assert vanish.wait(_STEP_TIMEOUT)
        _reset(connection)

    return talk


def _ping_often(client: httpx2.AsyncClient) -> AsyncWebSocketClient:
    return AsyncWebSocketClient(
        client,
        keepalive_ping_interval_seconds=0.05,
        keepalive_ping_timeout_seconds=None,
    )


async def _hold_a_subscription(
    url: str, vanish: threading.Event, hold: Callable[[], Awaitable[None]]
) -> None:
    client = AsyncGQLClient(base_url=url, subscription_protocol="graphql-transport-ws")
    try:
        async with client.subscribe(
            _Result, "subscription { counter }", variables={}, headers={}
        ):
            await asyncio.sleep(0.2)
            vanish.set()
            await hold()
    finally:
        await client.close()


# The sync session runs its keepalive in a thread, and what that thread fails
# with does not reach the caller: `httpx2` offers no way to see it.
def test_a_failed_keepalive_interrupts_an_async_subscription(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(runtime, "AsyncWebSocketClient", _ping_often)
    vanish = threading.Event()

    async def hold() -> None:
        await asyncio.sleep(_STEP_TIMEOUT)

    with (
        _tcp_server(_talk_then_vanish(vanish)) as url,
        pytest.raises(SubscriptionInterruptedError) as caught,
    ):
        run_async(_hold_a_subscription(url, vanish, hold))
    assert str(caught.value)
    assert isinstance(caught.value.__cause__, WebSocketNetworkError)


def test_independent_failures_of_an_async_websocket_come_as_a_group(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(runtime, "AsyncWebSocketClient", _ping_often)
    vanish = threading.Event()
    error = KeyError("caller")

    async def hold() -> None:
        try:
            await asyncio.sleep(_STEP_TIMEOUT)
        finally:
            raise error

    with (
        _tcp_server(_talk_then_vanish(vanish)) as url,
        pytest.raises(ExceptionGroup) as caught,
    ):
        run_async(_hold_a_subscription(url, vanish, hold))
    assert error in caught.value.exceptions
    [interruption] = [exc for exc in caught.value.exceptions if exc is not error]
    assert type(interruption) is SubscriptionInterruptedError
    assert isinstance(interruption.__cause__, WebSocketNetworkError)


# --- the README ------------------------------------------------------------


def test_the_response_of_an_error_keeps_its_request():
    options = GQLClientOptions(headers={"Authorization": "Bearer token"})
    with _tcp_server(_answering(_response(502, b"Bad Gateway"))) as url:
        error = _failure("sync", "query", url, options)
    assert isinstance(error, HTTPResponseError)
    assert error.response.request.headers["Authorization"] == "Bearer token"
    assert error.response.text == "Bad Gateway"


def _readme_retry() -> str:
    readme = Path(__file__).parent.parent / "README.md"
    blocks: list[str] = re.findall(
        r"```python\n(.*?)```", readme.read_text(encoding="utf-8"), re.DOTALL
    )
    [block] = [block for block in blocks if "get_user_patiently" in block]
    return block


# Runs the README's retry against a server that answers with `answers` in turn,
# and returns the outcome with the number of delays it waited.
async def _run_readme_retry(answers: list[bytes]) -> tuple[int | Exception, int]:
    left = list(answers)
    delays: list[float] = []

    def talk(connection: socket.socket) -> None:
        _answering(left.pop(0))(connection)

    class Operation:
        def __init__(self, url: str):
            self.url = url

        async def execute(self, **_variables: object) -> list[int]:
            return await _async_call("query", self.url, {})

    async def sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    with _tcp_server(talk) as url:
        namespace: dict[str, object] = {"GetUserQueryResult": list}
        exec(_readme_retry(), namespace)
        namespace["get_user"] = Operation(url)
        namespace["asyncio"] = SimpleNamespace(sleep=sleep)
        retry = cast(
            "Callable[[str], Awaitable[list[int]]]", namespace["get_user_patiently"]
        )
        try:
            [counter] = await retry("1")
        except GraphQLClientError as exc:
            return exc, len(delays)
        return counter, len(delays)


async def test_readme_retries_an_unreachable_and_an_unavailable_server():
    answers = [
        b"",
        _response(503, _REFUSAL),
        _response(503, b"unavailable", content_type="text/plain"),
        _response(200, b'{"data": {"counter": 1}}'),
    ]
    assert await _run_readme_retry(answers) == (1, 3)


@pytest.mark.parametrize("status", [200, 401, 500])
async def test_readme_does_not_retry_another_refusal(status: int):
    outcome, delays = await _run_readme_retry([_response(status, _REFUSAL)])
    assert isinstance(outcome, GraphQLResponseError)
    assert outcome.status_code == status
    assert delays == 0


async def test_readme_gives_up_after_its_last_delay():
    outcome, delays = await _run_readme_retry([b""] * 4)
    assert isinstance(outcome, TransportError)
    assert delays == 3
