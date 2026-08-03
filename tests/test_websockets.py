import asyncio
import json
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from dataclasses import dataclass
from dataclasses import field
from urllib.parse import parse_qs

import pydantic
import pytest

from iron_gql import FileVar
from iron_gql import GraphQLResponseError
from iron_gql import websockets
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.runtime import GQLOperation
from tests.conftest import generated_package
from tests.conftest import live_asgi_server
from tests.conftest import use_client

generated_package(
    "websockets_codegen",
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
    from tests.generated.websockets_codegen.gql.api import api_gql

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

from tests.generated.websockets_codegen import queries as codegen_queries

type _Event = MutableMapping[str, object]
type _Receive = Callable[[], Awaitable[_Event]]
type _Send = Callable[[_Event], Awaitable[None]]

_JSON_OBJECT = pydantic.TypeAdapter(dict[str, object])
_HEADER_PAIRS = pydantic.TypeAdapter(list[tuple[bytes, bytes]])


async def _receive_json(receive: _Receive) -> dict[str, object]:
    text = (await receive())["text"]
    assert isinstance(text, str)
    return _JSON_OBJECT.validate_json(text)


def _decode_headers(scope: MutableMapping[str, object]) -> list[tuple[str, str]]:
    return [
        (key.decode(), value.decode())
        for key, value in _HEADER_PAIRS.validate_python(scope["headers"])
    ]


@dataclass
class _Captured:
    scope_raw_headers: list[tuple[str, str]] = field(default_factory=list)
    scope_headers: dict[str, str] = field(default_factory=dict)
    scope_query_string: str = ""
    subscribe: dict[str, object] | None = None
    client_responses: list[dict[str, object]] = field(default_factory=list)


def _make_ws_app(  # noqa: C901
    messages: list[dict[str, object]],
    *,
    pre_ack_messages: list[dict[str, object]] | None = None,
    ack_response: dict[str, str] | None = None,
    captured: _Captured | None = None,
) -> ASGIApp:
    async def app(  # noqa: C901
        scope: MutableMapping[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        if captured is not None:
            scope_headers = _decode_headers(scope)
            captured.scope_raw_headers = scope_headers
            captured.scope_headers = dict(scope_headers)
            query_string = scope["query_string"]
            assert isinstance(query_string, bytes)
            captured.scope_query_string = query_string.decode()
        subprotocols = scope["subprotocols"]
        assert isinstance(subprotocols, list)
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })

        init_msg = await _receive_json(receive)
        assert init_msg["type"] == "connection_init"
        for msg in pre_ack_messages or []:
            await send({
                "type": "websocket.send",
                "text": json.dumps(msg),
            })
            if msg.get("type") == "ping":
                pong_msg = await _receive_json(receive)
                if captured is not None:
                    captured.client_responses.append(pong_msg)
        ack = ack_response or {"type": "connection_ack"}
        await send({
            "type": "websocket.send",
            "text": json.dumps(ack),
        })

        if ack["type"] != "connection_ack":
            await receive()
            return

        subscribe_msg = await _receive_json(receive)
        assert subscribe_msg["type"] == "subscribe"
        sub_id = subscribe_msg["id"]
        if captured is not None:
            captured.subscribe = subscribe_msg

        for msg in messages:
            if msg.get("type") == "ping":
                await send({
                    "type": "websocket.send",
                    "text": json.dumps(msg),
                })
                pong_msg = await _receive_json(receive)
                if captured is not None:
                    captured.client_responses.append(pong_msg)
            elif msg.get("type") == "pong":
                await send({
                    "type": "websocket.send",
                    "text": json.dumps(msg),
                })
            else:
                await send({
                    "type": "websocket.send",
                    "text": json.dumps({"id": sub_id, **msg}),
                })

        await receive()

    return app


def _make_ws_disconnect_app(
    messages_before_close: list[dict[str, object]],
    close_code: int,
    close_reason: str = "",
) -> ASGIApp:
    async def app(
        scope: MutableMapping[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope["subprotocols"]
        assert isinstance(subprotocols, list)
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = await _receive_json(receive)
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.send",
            "text": json.dumps({"type": "connection_ack"}),
        })
        subscribe_msg = await _receive_json(receive)
        assert subscribe_msg["type"] == "subscribe"
        sub_id = subscribe_msg["id"]

        for msg in messages_before_close:
            await send({
                "type": "websocket.send",
                "text": json.dumps({"id": sub_id, **msg}),
            })

        await send({
            "type": "websocket.close",
            "code": close_code,
            "reason": close_reason,
        })

    return app


class _CounterResult(pydantic.BaseModel):
    counter: int


class _PingResult(pydantic.BaseModel):
    ping: str


async def test_subscribe_asgi():
    app = _make_ws_app([
        {"type": "next", "payload": {"data": {"counter": 1}}},
        {"type": "next", "payload": {"data": {"counter": 2}}},
        {"type": "next", "payload": {"data": {"counter": 3}}},
        {"type": "complete"},
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }", variables={}, headers={}
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1, 2, 3]
    finally:
        await client.close()


async def test_subscribe_error():
    app = _make_ws_app([
        {"type": "next", "payload": {"data": {"counter": 1}}},
        {"type": "error", "payload": [{"message": "boom"}]},
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        results: list[int] = []

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for item in stream:
                    results.append(item.counter)  # noqa: PERF401

        with pytest.raises(GraphQLResponseError, match="boom"):
            await consume()

        assert results == [1]
    finally:
        await client.close()


async def test_subscribe_next_with_errors():
    app = _make_ws_app([
        {
            "type": "next",
            "payload": {"data": None, "errors": [{"message": "partial"}]},
        },
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="partial"):
            await consume()
    finally:
        await client.close()


def test_gql_operation_with_headers():
    op = GQLOperation()
    assert op.headers == {}

    op2 = op.with_headers({"Authorization": "Bearer token"})
    assert op2.headers == {"Authorization": "Bearer token"}
    assert op.headers == {}


async def test_subscribe_codegen_asgi(monkeypatch: pytest.MonkeyPatch):
    messages: list[dict[str, object]] = [
        {
            "type": "next",
            "payload": {"data": {"events": {"id": "1", "message": "hello"}}},
        },
        {
            "type": "next",
            "payload": {"data": {"events": {"id": "2", "message": "world"}}},
        },
        {"type": "complete"},
    ]

    captured = _Captured()
    app = _make_ws_app(messages, captured=captured)

    async with use_client(
        monkeypatch,
        "websockets_codegen",
        "http://testserver/graphql",
        target_app=app,
    ):
        async with codegen_queries.events.execute(channel="test") as stream:
            results = [(item.events.id, item.events.message) async for item in stream]
        assert results == [("1", "hello"), ("2", "world")]
        assert captured.subscribe is not None
        payload = captured.subscribe["payload"]
        assert isinstance(payload, dict)
        assert payload["variables"] == {"channel": "test"}


async def test_subscribe_connection_rejected():
    app = _make_ws_app(
        [],
        ack_response={"type": "connection_error", "payload": "Unauthorized"},
    )

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="Expected connection_ack"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_no_data_in_next():
    app = _make_ws_app([
        {"type": "next", "payload": {"data": None}},
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="No data in response"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_unexpected_message_type():
    app = _make_ws_app([
        {"type": "unknown_type"},
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="Unexpected subscription message type"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_with_headers_propagation():
    captured = _Captured()
    app = _make_ws_app(
        [{"type": "complete"}],
        captured=captured,
    )

    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        target_app=app,
        headers={"X-Base": "base-value"},
    )
    try:
        async with client.subscribe(
            _CounterResult,
            "subscription { counter }",
            variables={},
            headers={"Authorization": "Bearer token"},
        ) as stream:
            async for _ in stream:
                pass
        assert captured.scope_headers["authorization"] == "Bearer token"
        assert captured.scope_headers["x-base"] == "base-value"
    finally:
        await client.close()


async def test_subscribe_ping_pong():
    captured = _Captured()
    app = _make_ws_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
            {"type": "ping"},
            {"type": "next", "payload": {"data": {"counter": 2}}},
            {"type": "next", "payload": {"data": {"counter": 3}}},
            {"type": "complete"},
        ],
        captured=captured,
    )

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }", variables={}, headers={}
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1, 2, 3]
        assert captured.client_responses == [{"type": "pong"}]
    finally:
        await client.close()


async def test_subscribe_ping_before_connection_ack():
    captured = _Captured()
    app = _make_ws_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
            {"type": "complete"},
        ],
        pre_ack_messages=[{"type": "ping"}],
        captured=captured,
    )

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }", variables={}, headers={}
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1]
        assert captured.client_responses == [{"type": "pong"}]
    finally:
        await client.close()


async def test_subscribe_with_headers_override_case_insensitive():
    captured = _Captured()
    app = _make_ws_app([{"type": "complete"}], captured=captured)

    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        target_app=app,
        headers={"Authorization": "Bearer base-token"},
    )
    try:
        async with client.subscribe(
            _CounterResult,
            "subscription { counter }",
            variables={},
            headers={"authorization": "Bearer override-token"},
        ) as stream:
            async for _ in stream:
                pass
        authorization_headers = [
            value for key, value in captured.scope_raw_headers if key == "authorization"
        ]
        assert authorization_headers == ["Bearer override-token"]
    finally:
        await client.close()


async def test_subscribe_carries_http_cookies():
    captured = _Captured()

    async def app(
        scope: MutableMapping[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope["type"] == "http":
            request = await receive()
            assert request["type"] == "http.request"
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"set-cookie", b"session=abc; Path=/; HttpOnly"],
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"data":{"ping":"pong"}}',
            })
            return

        assert scope["type"] == "websocket"
        captured.scope_headers = dict(_decode_headers(scope))
        subprotocols = scope["subprotocols"]
        assert isinstance(subprotocols, list)
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = await _receive_json(receive)
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.send",
            "text": json.dumps({"type": "connection_ack"}),
        })
        subscribe_msg = await _receive_json(receive)
        assert subscribe_msg["type"] == "subscribe"
        await send({
            "type": "websocket.send",
            "text": json.dumps({"id": subscribe_msg["id"], "type": "complete"}),
        })
        await receive()

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        result = await client.query(
            _PingResult, "query { ping }", variables={}, headers={}
        )
        assert result.ping == "pong"

        async with client.subscribe(
            _CounterResult, "subscription { counter }", variables={}, headers={}
        ) as stream:
            async for _ in stream:
                pass

        assert "session=abc" in captured.scope_headers["cookie"]
    finally:
        await client.close()


async def test_subscribe_disconnect_before_connection_ack():
    async def app(
        scope: MutableMapping[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope["subprotocols"]
        assert isinstance(subprotocols, list)
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = await _receive_json(receive)
        assert init_msg["type"] == "connection_init"
        await send({"type": "websocket.close", "code": 4401, "reason": "Unauthorized"})

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="before connection_ack with code 4401"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_connection_ack_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(websockets, "_WS_CONNECTION_ACK_TIMEOUT_SECONDS", 0.01)

    async def app(
        scope: MutableMapping[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope["subprotocols"]
        assert isinstance(subprotocols, list)
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = await _receive_json(receive)
        assert init_msg["type"] == "connection_init"
        await asyncio.sleep(0.1)

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="Timed out waiting for connection_ack"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_url_scheme_https_to_wss():
    captured = _Captured()
    app = _make_ws_app([{"type": "complete"}], captured=captured)

    client = AsyncGQLClient(
        base_url="https://testserver/graphql?redirect=http://callback",
        target_app=app,
    )
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }", variables={}, headers={}
        ) as stream:
            async for _ in stream:
                pass
        assert parse_qs(captured.scope_query_string)["redirect"] == ["http://callback"]
    finally:
        await client.close()


async def test_subscribe_malformed_message_no_type():
    app = _make_ws_app([
        {"payload": "no type field here"},
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="Unexpected subscription message type: None"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_no_variables_key_when_none():
    captured = _Captured()
    app = _make_ws_app([{"type": "complete"}], captured=captured)

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }", variables={}, headers={}
        ) as stream:
            async for _ in stream:
                pass
        assert captured.subscribe is not None
        payload = captured.subscribe["payload"]
        assert isinstance(payload, dict)
        assert "variables" not in payload
    finally:
        await client.close()


async def test_subscribe_error_without_payload():
    app = _make_ws_app([
        {"type": "error"},
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="Error without payload"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_pong_during_messages():
    app = _make_ws_app([
        {"type": "next", "payload": {"data": {"counter": 1}}},
        {"type": "pong"},
        {"type": "next", "payload": {"data": {"counter": 2}}},
        {"type": "complete"},
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }", variables={}, headers={}
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1, 2]
    finally:
        await client.close()


async def test_subscribe_pong_before_connection_ack():
    captured = _Captured()
    app = _make_ws_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
            {"type": "complete"},
        ],
        pre_ack_messages=[{"type": "pong"}],
        captured=captured,
    )

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }", variables={}, headers={}
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1]
    finally:
        await client.close()


async def test_subscribe_next_without_payload_key():
    app = _make_ws_app([
        {"type": "next"},
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="No data in response"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_pre_ack_messages_exhaustion():
    pong: dict[str, object] = {"type": "pong"}
    app = _make_ws_app(
        [],
        pre_ack_messages=[pong] * 17,
    )

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="No connection_ack after 16 messages"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_unsupported_url_scheme():
    client = AsyncGQLClient(base_url="ftp://testserver/graphql", target_app=None)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            ValueError, match="Unsupported URL scheme for WebSocket subscription: ftp"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_next_with_data_and_errors():
    app = _make_ws_app([
        {
            "type": "next",
            "payload": {
                "data": {"counter": 1},
                "errors": [{"message": "partial failure"}],
            },
        },
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="partial failure"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_normal_closure_during_messages():
    app = _make_ws_disconnect_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
            {"type": "next", "payload": {"data": {"counter": 2}}},
        ],
        close_code=1000,
    )

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }", variables={}, headers={}
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1, 2]
    finally:
        await client.close()


async def test_subscribe_abnormal_disconnect_during_messages():
    app = _make_ws_disconnect_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
        ],
        close_code=1011,
        close_reason="Internal Error",
    )

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        results: list[int] = []

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for item in stream:
                    results.append(item.counter)  # noqa: PERF401

        with pytest.raises(
            GraphQLResponseError, match="disconnected with code 1011: Internal Error"
        ):
            await consume()

        assert results == [1]
    finally:
        await client.close()


async def test_subscribe_disconnect_before_ack_with_reason():
    async def app(
        scope: MutableMapping[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope["subprotocols"]
        assert isinstance(subprotocols, list)
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = await _receive_json(receive)
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.close",
            "code": 4401,
            "reason": "Unauthorized",
        })

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError,
            match="before connection_ack with code 4401: Unauthorized",
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_invalid_json_during_messages():
    async def app(
        scope: MutableMapping[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope["subprotocols"]
        assert isinstance(subprotocols, list)
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = await _receive_json(receive)
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.send",
            "text": json.dumps({"type": "connection_ack"}),
        })
        subscribe_msg = await _receive_json(receive)
        assert subscribe_msg["type"] == "subscribe"
        await send({
            "type": "websocket.send",
            "text": "not valid json{{{",
        })
        await receive()

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="Server sent invalid JSON"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_invalid_json_during_handshake():
    async def app(
        scope: MutableMapping[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope["subprotocols"]
        assert isinstance(subprotocols, list)
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = await _receive_json(receive)
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.send",
            "text": "<<<broken>>>",
        })
        await receive()

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="Server sent invalid JSON during handshake"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_validation_error():
    # The same contract as the query path: result validation failures are
    # pydantic.ValidationError with a response path on every transport.
    app = _make_ws_app([
        {"type": "next", "payload": {"data": {"counter": "not_an_int"}}},
    ])

    client = AsyncGQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(pydantic.ValidationError) as exc_info:
            await consume()
        assert exc_info.value.errors()[0]["loc"] == ("counter",)
    finally:
        await client.close()


def test_sync_subscribe_streams_messages():
    captured = _Captured()
    app = _make_ws_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
            {"type": "next", "payload": {"data": {"counter": 2}}},
            {"type": "complete"},
        ],
        captured=captured,
    )
    with live_asgi_server(app) as base_url:
        client = GQLClient(base_url=base_url, headers={"X-Default": "on"})
        try:
            with client.subscribe(
                _CounterResult,
                "subscription { counter }",
                variables={},
                headers={"X-Call": "yes"},
            ) as stream:
                counters = [event.counter for event in stream]
        finally:
            client.close()

    assert counters == [1, 2]
    assert captured.scope_headers["x-default"] == "on"
    assert captured.scope_headers["x-call"] == "yes"
    assert captured.subscribe is not None
    assert captured.subscribe["payload"] == {"query": "subscription { counter }"}


def test_sync_subscribe_answers_ping():
    captured = _Captured()
    app = _make_ws_app(
        [{"type": "ping"}, {"type": "next", "payload": {"data": {"counter": 7}}}],
        captured=captured,
    )
    with live_asgi_server(app) as base_url:
        client = GQLClient(base_url=base_url)
        try:
            with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                assert next(stream).counter == 7
        finally:
            client.close()

    assert captured.client_responses == [{"type": "pong"}]


def test_sync_subscribe_raises_on_abnormal_close():
    app = _make_ws_disconnect_app([], 4400, "bad request")
    with live_asgi_server(app) as base_url:
        client = GQLClient(base_url=base_url)
        try:
            with (
                pytest.raises(GraphQLResponseError, match="bad request"),
                client.subscribe(
                    _CounterResult,
                    "subscription { counter }",
                    variables={},
                    headers={},
                ) as stream,
            ):
                list(stream)
        finally:
            client.close()


def test_sync_subscribe_rejects_file_variables():
    client = GQLClient(base_url="http://127.0.0.1:1/graphql")
    try:
        with (
            pytest.raises(TypeError, match="File uploads are not supported"),
            client.subscribe(
                _CounterResult,
                "subscription { counter }",
                variables={"file": FileVar(b"x")},
                headers={},
            ),
        ):
            pass
    finally:
        client.close()


def _make_ws_raw_app(
    *,
    pre_ack_texts: tuple[str, ...] = (),
    ack: bool = True,
    post_subscribe_texts: tuple[str, ...] = (),
    close_after_init: tuple[int, str] | None = None,
) -> ASGIApp:
    async def app(
        scope: MutableMapping[str, object],
        receive: _Receive,
        send: _Send,
    ) -> None:
        assert scope["type"] == "websocket"
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws",
        })
        init_msg = await _receive_json(receive)
        assert init_msg["type"] == "connection_init"
        if close_after_init is not None:
            code, reason = close_after_init
            await send({"type": "websocket.close", "code": code, "reason": reason})
            return
        for text in pre_ack_texts:
            await send({"type": "websocket.send", "text": text})
        if not ack:
            await receive()
            return
        await send({
            "type": "websocket.send",
            "text": json.dumps({"type": "connection_ack"}),
        })
        subscribe_msg = await _receive_json(receive)
        assert subscribe_msg["type"] == "subscribe"
        for text in post_subscribe_texts:
            await send({"type": "websocket.send", "text": text})
        await receive()

    return app


def _sync_subscribe_error(app: ASGIApp, match: str) -> None:
    with live_asgi_server(app) as base_url:
        client = GQLClient(base_url=base_url)
        try:
            with (
                pytest.raises(GraphQLResponseError, match=match),
                client.subscribe(
                    _CounterResult,
                    "subscription { counter }",
                    variables={},
                    headers={},
                ) as stream,
            ):
                list(stream)
        finally:
            client.close()


def test_sync_subscribe_answers_ping_before_connection_ack():
    captured = _Captured()
    app = _make_ws_app(
        [{"type": "complete"}],
        pre_ack_messages=[{"type": "ping"}],
        captured=captured,
    )
    with live_asgi_server(app) as base_url:
        client = GQLClient(base_url=base_url)
        try:
            with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                assert list(stream) == []
        finally:
            client.close()

    assert captured.client_responses == [{"type": "pong"}]


def test_sync_subscribe_ignores_pong_before_connection_ack():
    app = _make_ws_app([{"type": "complete"}], pre_ack_messages=[{"type": "pong"}])
    with live_asgi_server(app) as base_url:
        client = GQLClient(base_url=base_url)
        try:
            with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                assert list(stream) == []
        finally:
            client.close()


def test_sync_subscribe_ignores_pong_during_messages():
    app = _make_ws_app([
        {"type": "pong"},
        {"type": "next", "payload": {"data": {"counter": 3}}},
        {"type": "complete"},
    ])
    with live_asgi_server(app) as base_url:
        client = GQLClient(base_url=base_url)
        try:
            with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                assert [event.counter for event in stream] == [3]
        finally:
            client.close()


def test_sync_subscribe_stops_on_normal_closure():
    app = _make_ws_disconnect_app(
        [{"type": "next", "payload": {"data": {"counter": 5}}}], 1000
    )
    with live_asgi_server(app) as base_url:
        client = GQLClient(base_url=base_url)
        try:
            with client.subscribe(
                _CounterResult, "subscription { counter }", variables={}, headers={}
            ) as stream:
                assert [event.counter for event in stream] == [5]
        finally:
            client.close()


def test_sync_subscribe_pre_ack_messages_exhaustion():
    app = _make_ws_raw_app(
        pre_ack_texts=tuple(json.dumps({"type": "pong"}) for _ in range(16)),
        ack=False,
    )
    _sync_subscribe_error(app, "No connection_ack after 16 messages")


def test_sync_subscribe_disconnect_before_connection_ack():
    app = _make_ws_raw_app(close_after_init=(4401, "unauthorized"))
    _sync_subscribe_error(app, "disconnected before connection_ack.*unauthorized")


def test_sync_subscribe_invalid_json_during_handshake():
    app = _make_ws_raw_app(pre_ack_texts=("not valid json{{{",), ack=False)
    _sync_subscribe_error(app, "Server sent invalid JSON during handshake")


def test_sync_subscribe_malformed_message_during_handshake():
    app = _make_ws_raw_app(pre_ack_texts=(json.dumps("plain string"),), ack=False)
    _sync_subscribe_error(app, "Malformed protocol message during handshake")


def test_sync_subscribe_connection_ack_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(websockets, "_WS_CONNECTION_ACK_TIMEOUT_SECONDS", 0.05)
    app = _make_ws_raw_app(ack=False)
    _sync_subscribe_error(app, "Timed out waiting for connection_ack")


def test_sync_subscribe_invalid_json_during_messages():
    app = _make_ws_raw_app(post_subscribe_texts=("not valid json{{{",))
    _sync_subscribe_error(app, "Server sent invalid JSON")


def test_sync_subscribe_malformed_message_during_messages():
    app = _make_ws_raw_app(post_subscribe_texts=(json.dumps(42),))
    _sync_subscribe_error(app, "Malformed protocol message")
