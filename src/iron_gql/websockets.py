from collections.abc import AsyncGenerator
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from typing import Literal

import httpx2
import pydantic
from httpx2.websockets import AsyncWebSocketSession
from httpx2.websockets import WebSocketSession
from wsproto.utilities import LocalProtocolError

from iron_gql.call import CallLimits
from iron_gql.errors import GraphQLResponseError
from iron_gql.errors import ResponseProtocolError
from iron_gql.errors import SubscriptionInterruptedError
from iron_gql.errors import TransportError
from iron_gql.protocol import ProtocolModels
from iron_gql.protocol import WSMessage
from iron_gql.slots import SlotReaders
from iron_gql.subscription import Emit
from iron_gql.subscription import malformed_next_error
from iron_gql.subscription import next_result
from iron_gql.transport import translated

_MAX_PRE_ACK_MESSAGES = 16


# How the failures of a session read before `subscribe` goes out, when a broken
# connection leaves a subscription the server was never asked for, and after.
@dataclass(frozen=True, slots=True, kw_only=True)
class _Stage:
    action: str
    broken: type[TransportError]
    malformed: str
    awaited: str


_HANDSHAKE = _Stage(
    action="The WebSocket handshake",
    broken=TransportError,
    malformed="Malformed protocol message during handshake",
    awaited="connection_ack",
)
_STREAM = _Stage(
    action="The subscription",
    broken=SubscriptionInterruptedError,
    malformed="Malformed protocol message",
    awaited="the next message",
)


# What a received protocol message asks the caller to do. Only "pong" needs IO,
# which is why the decision is separated from the transport at all: the async
# and sync receive loops differ in nothing else. Each loop reads a frame as text
# and parses it with the message model in one step, so a frame that is not JSON
# is as malformed as one that is JSON of the wrong shape.
type MessageAction[T: pydantic.BaseModel] = Emit[T] | Literal["pong", "skip", "stop"]


def _handshake_action(message: WSMessage) -> Literal["ack", "pong", "skip"]:
    match message.type:
        case "connection_ack":
            return "ack"
        case "ping":
            return "pong"
        case "pong":
            return "skip"
        case _:
            msg = f"Expected connection_ack, got {message}"
            raise ResponseProtocolError(msg, status_code=None)


def _message_action[T: pydantic.BaseModel](
    message: WSMessage,
    result_type: type[T],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> MessageAction[T]:
    match message.type:
        case "next":
            try:
                payload = models.next_payload.model_validate(message.payload or {})
            except pydantic.ValidationError as exc:
                raise malformed_next_error(exc, None) from exc
            return Emit(next_result(payload, result_type, slot_readers, None))
        case "error":
            try:
                error_payload = models.ws_error.model_validate({
                    "errors": message.payload
                }).errors
            except pydantic.ValidationError as exc:
                msg = f"Malformed error payload: {exc}"
                raise ResponseProtocolError(msg, status_code=None) from exc
            if not error_payload:
                msg = f"Error without payload: {message}"
                raise ResponseProtocolError(msg, status_code=None)
            raise GraphQLResponseError(error_payload, status_code=None)
        case "complete":
            return "stop"
        case "ping":
            return "pong"
        case "pong":
            return "skip"
        case _:
            detail = f"type: {message.type!r}, message: {message}"
            msg = f"Unexpected subscription message {detail}"
            raise ResponseProtocolError(msg, status_code=None)


def _parsed(text: str, models: ProtocolModels, stage: _Stage) -> WSMessage:
    try:
        return models.ws_message.model_validate_json(text)
    except pydantic.ValidationError as exc:
        msg = f"{stage.malformed}: {exc}"
        raise ResponseProtocolError(msg, status_code=None) from exc


@contextmanager
def _receiving(stage: _Stage, limit: float | None) -> Generator[None]:
    try:
        with translated(stage.action, broken=stage.broken):
            yield
    except TimeoutError as exc:
        msg = f"Timed out waiting for {stage.awaited} after {limit} seconds"
        raise stage.broken(msg) from exc


def _no_ack() -> ResponseProtocolError:
    limit = _MAX_PRE_ACK_MESSAGES
    msg = f"No connection_ack within {limit} messages, the limit of the client"
    return ResponseProtocolError(msg, status_code=None)


def ws_url(url: httpx2.URL) -> httpx2.URL:
    match url.scheme:
        case "https":
            return url.copy_with(scheme="wss")
        case "http":
            return url.copy_with(scheme="ws")
        case "ws" | "wss":
            return url
        case scheme:
            msg = f"Unsupported URL scheme for WebSocket subscription: {scheme}"
            raise ValueError(msg)


# Whether the message went out. wsproto, which comes with `httpx2[ws]` and whose
# exceptions a session lets through, refuses to send on a connection that is
# closing, and by then the close that says why is there to be read, behind the
# messages that came before it: the caller reads on.
async def _async_send(
    ws: AsyncWebSocketSession, message: dict[str, Any], stage: _Stage
) -> bool:
    try:
        with translated(stage.action, broken=stage.broken):
            await ws.send_json(message)
    except LocalProtocolError:
        return False
    return True


async def _async_receive(
    ws: AsyncWebSocketSession, stage: _Stage, limit: float | None
) -> str:
    with _receiving(stage, limit):
        return await ws.receive_text(timeout=limit)


async def async_graphql_ws_subscribe[T: pydantic.BaseModel](
    ws: AsyncWebSocketSession,
    result_type: type[T],
    payload: dict[str, Any],
    limits: CallLimits,
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> AsyncGenerator[T]:
    await _async_ws_handshake(ws, limits.timeout, models)
    subscribe = {"id": "1", "type": "subscribe", "payload": payload}
    if not await _async_send(ws, subscribe, _HANDSHAKE):
        while True:
            await _async_receive(ws, _HANDSHAKE, None)
    return _async_ws_receive_messages(
        ws, result_type, limits.stream_idle_timeout, slot_readers, models
    )


async def _async_ws_handshake(
    ws: AsyncWebSocketSession, limit: float | None, models: ProtocolModels
) -> None:
    await _async_send(ws, {"type": "connection_init"}, _HANDSHAKE)
    for _ in range(_MAX_PRE_ACK_MESSAGES):
        text = await _async_receive(ws, _HANDSHAKE, limit)
        match _handshake_action(_parsed(text, models, _HANDSHAKE)):
            case "ack":
                return
            case "pong":
                await _async_send(ws, {"type": "pong"}, _HANDSHAKE)
            case "skip":
                pass
    raise _no_ack()


async def _async_ws_receive_messages[T: pydantic.BaseModel](
    ws: AsyncWebSocketSession,
    result_type: type[T],
    idle_limit: float | None,
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> AsyncGenerator[T]:
    while True:
        text = await _async_receive(ws, _STREAM, idle_limit)
        message = _parsed(text, models, _STREAM)
        match _message_action(message, result_type, slot_readers, models):
            case Emit(value=value):
                yield value
            case "pong":
                await _async_send(ws, {"type": "pong"}, _STREAM)
            case "skip":
                pass
            case "stop":
                return


def _send(ws: WebSocketSession, message: dict[str, Any], stage: _Stage) -> bool:
    try:
        with translated(stage.action, broken=stage.broken):
            ws.send_json(message)
    except LocalProtocolError:
        return False
    return True


def _receive(ws: WebSocketSession, stage: _Stage, limit: float | None) -> str:
    with _receiving(stage, limit):
        return ws.receive_text(timeout=limit)


def graphql_ws_subscribe[T: pydantic.BaseModel](
    ws: WebSocketSession,
    result_type: type[T],
    payload: dict[str, Any],
    limits: CallLimits,
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> Generator[T]:
    _ws_handshake(ws, limits.timeout, models)
    subscribe = {"id": "1", "type": "subscribe", "payload": payload}
    if not _send(ws, subscribe, _HANDSHAKE):
        while True:
            _receive(ws, _HANDSHAKE, None)
    return _ws_receive_messages(
        ws, result_type, limits.stream_idle_timeout, slot_readers, models
    )


def _ws_handshake(
    ws: WebSocketSession, limit: float | None, models: ProtocolModels
) -> None:
    _send(ws, {"type": "connection_init"}, _HANDSHAKE)
    for _ in range(_MAX_PRE_ACK_MESSAGES):
        text = _receive(ws, _HANDSHAKE, limit)
        match _handshake_action(_parsed(text, models, _HANDSHAKE)):
            case "ack":
                return
            case "pong":
                _send(ws, {"type": "pong"}, _HANDSHAKE)
            case "skip":
                pass
    raise _no_ack()


def _ws_receive_messages[T: pydantic.BaseModel](
    ws: WebSocketSession,
    result_type: type[T],
    idle_limit: float | None,
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> Generator[T]:
    while True:
        text = _receive(ws, _STREAM, idle_limit)
        message = _parsed(text, models, _STREAM)
        match _message_action(message, result_type, slot_readers, models):
            case Emit(value=value):
                yield value
            case "pong":
                _send(ws, {"type": "pong"}, _STREAM)
            case "skip":
                pass
            case "stop":
                return
