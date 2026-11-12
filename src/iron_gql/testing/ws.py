import json

import pydantic

from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend

SUBPROTOCOL = "graphql-transport-ws"

_JSON_OBJECT = pydantic.TypeAdapter(dict[str, object])


# A disconnect, and a binary frame, carry no text: ASGI leaves the key out.
async def _receive_json(receive: ASGIReceive) -> dict[str, object]:
    event = await receive()
    text = event.get("text")
    assert isinstance(text, str), f"expected a text frame, got {event!r}"  # noqa: S101
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
        return await _expect(self, "pong")

    # Acknowledges the connection and waits for the client's `subscribe`.
    async def ack(self) -> "WSTestSubscription":
        await self.send_message({"type": "connection_ack"})
        message = await _expect(self, "subscribe")
        subscription_id = message.get("id")
        assert isinstance(subscription_id, str), (  # noqa: S101
            f"subscribe carried no string id: {message!r}"
        )
        payload = message.get("payload")
        assert isinstance(payload, dict), (  # noqa: S101
            f"subscribe carried no payload object: {message!r}"
        )
        # The assertion narrows to a dict of unknown items; validating it again
        # carries the type the subscription holds.
        return WSTestSubscription(
            self, subscription_id, _JSON_OBJECT.validate_python(payload)
        )

    async def close(self, code: int, reason: str = "") -> None:
        await self._send({"type": "websocket.close", "code": code, "reason": reason})

    # Waits for the client to hang up. A fake that returns before the client
    # disconnects tears the connection down under it.
    # Whatever the client sends before it hangs up is dropped.
    async def drain(self) -> None:
        while (await self._receive())["type"] != "websocket.disconnect":
            pass


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
        # The `payload` of the client's `subscribe`: the query and variables it
        # asked for.
        self.payload = payload

    async def send_message(self, message: dict[str, object]) -> None:
        await self.connection.send_message({**message, "id": self.id})

    async def next(self, data: dict[str, object]) -> None:
        await self.send_message({"type": "next", "payload": {"data": data}})

    async def error(self, errors: list[dict[str, object]]) -> None:
        await self.send_message({"type": "error", "payload": errors})

    async def complete(self) -> None:
        await self.send_message({"type": "complete"})


async def _expect(connection: WSTestConnection, message_type: str) -> dict[str, object]:
    message = await connection.receive_message()
    assert message.get("type") == message_type, (  # noqa: S101
        f"expected {message_type}, got {message!r}"
    )
    return message


# Accepts a websocket connection and consumes the client's `connection_init`.
# The subprotocol is echoed only when the client offered it, so a client that
# forgets to ask for graphql-transport-ws is not silently handed one.
async def accept_graphql_ws(
    scope: ASGIScope, receive: ASGIReceive, send: ASGISend
) -> WSTestConnection:
    assert scope["type"] == "websocket", (  # noqa: S101
        f"expected a websocket scope, got {scope['type']!r}"
    )
    # ASGI makes `subprotocols` optional: a missing key means none were offered.
    subprotocols = scope.get("subprotocols", [])
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
    await _expect(connection, "connection_init")
    return connection
