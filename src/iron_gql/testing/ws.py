import json

import pydantic

from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend

SUBPROTOCOL = "graphql-transport-ws"

_JSON_OBJECT = pydantic.TypeAdapter(dict[str, object])


async def _receive_json(receive: ASGIReceive) -> dict[str, object]:
    text = (await receive())["text"]
    assert isinstance(text, str), f"expected a text frame, got {text!r}"  # noqa: S101
    return _JSON_OBJECT.validate_json(text)


# The server side of graphql-transport-ws, as a set of steps a fake drives
# itself rather than an app it configures. Control flow stays in the fake, so a
# stateful one — a connection counter, a drop on the N-th connect, a rejection
# of the first M mutations — is written as ordinary Python around these calls.
#
# Every step asserts the client held up its end of the protocol, and the
# assertion carries what actually arrived: a test fails on the step the client
# deviated at, not later on a missing message.
class WSTestConnection:
    def __init__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend):
        self.scope = scope
        self._receive = receive
        self._send = send

    async def send_message(self, message: dict[str, object]) -> None:
        await self._send({
            "type": "websocket.send",
            "text": json.dumps(message),
        })

    async def receive_message(self) -> dict[str, object]:
        return await _receive_json(self._receive)

    # A `ping` obliges the client to answer `pong`; reading it back keeps the
    # exchange in step and hands the test the payload the client echoed.
    async def expect_pong(self) -> dict[str, object]:
        message = await self.receive_message()
        expected = "pong"
        assert message["type"] == expected, (  # noqa: S101
            f"expected pong, got {message!r}"
        )
        return message

    # Acknowledges the connection and waits for the client's `subscribe`.
    async def ack(self) -> "WSTestSubscription":
        await self.send_message({"type": "connection_ack"})
        message = await self.receive_message()
        assert message["type"] == "subscribe", (  # noqa: S101
            f"expected subscribe, got {message!r}"
        )
        subscription_id = message["id"]
        assert isinstance(subscription_id, str), (  # noqa: S101
            f"subscribe carried a non-string id: {subscription_id!r}"
        )
        return WSTestSubscription(self, subscription_id, message)

    async def close(self, code: int, reason: str = "") -> None:
        await self._send({"type": "websocket.close", "code": code, "reason": reason})

    # Waits for the client to hang up. A fake that returns before the client
    # disconnects tears the connection down under it.
    async def drain(self) -> None:
        await self._receive()


# One subscription of a connection: everything it sends carries the id the
# client chose, so a fake never threads that id through itself.
class WSTestSubscription:
    def __init__(
        self,
        connection: WSTestConnection,
        subscription_id: str,
        payload: dict[str, object],
    ):
        self.connection = connection
        self.id = subscription_id
        # The `subscribe` message as received — its `payload` holds the query,
        # variables and operationName the client sent.
        self.payload = payload

    async def send_message(self, message: dict[str, object]) -> None:
        await self.connection.send_message({"id": self.id, **message})

    async def next(self, data: dict[str, object]) -> None:
        await self.send_message({"type": "next", "payload": {"data": data}})

    async def error(self, errors: list[dict[str, object]]) -> None:
        await self.send_message({"type": "error", "payload": errors})

    async def complete(self) -> None:
        await self.send_message({"type": "complete"})


# Accepts a websocket connection and consumes the client's `connection_init`.
# The subprotocol is echoed only when the client offered it, so a client that
# forgets to ask for graphql-transport-ws is not silently handed one.
async def accept_graphql_ws(
    scope: ASGIScope, receive: ASGIReceive, send: ASGISend
) -> WSTestConnection:
    assert scope["type"] == "websocket", (  # noqa: S101
        f"expected a websocket scope, got {scope['type']!r}"
    )
    subprotocols = scope["subprotocols"]
    assert isinstance(subprotocols, list), (  # noqa: S101
        f"scope carried no subprotocol list: {subprotocols!r}"
    )
    event = await receive()
    assert event["type"] == "websocket.connect", (  # noqa: S101
        f"expected websocket.connect, got {event!r}"
    )
    await send({
        "type": "websocket.accept",
        "subprotocol": SUBPROTOCOL if SUBPROTOCOL in subprotocols else None,
    })
    connection = WSTestConnection(scope, receive, send)
    message = await connection.receive_message()
    assert message["type"] == "connection_init", (  # noqa: S101
        f"expected connection_init, got {message!r}"
    )
    return connection
