import asyncio
import json
from collections.abc import AsyncGenerator
from collections.abc import Generator
from contextlib import asynccontextmanager
from contextlib import contextmanager
from typing import Any
from typing import Literal

import httpx2
import pydantic
from httpx2.websockets import AsyncWebSocketSession
from httpx2.websockets import WebSocketDisconnect
from httpx2.websockets import WebSocketSession

from iron_gql.errors import GraphQLResponseError
from iron_gql.protocol import ProtocolModels
from iron_gql.protocol import WSMessage
from iron_gql.slots import SlotReaders
from iron_gql.subscription import Emit
from iron_gql.subscription import malformed_next_error
from iron_gql.subscription import next_result

_MAX_PRE_ACK_MESSAGES = 16
_WS_CONNECTION_ACK_TIMEOUT_SECONDS = 10
_WS_NORMAL_CLOSURE = 1000


# What a received protocol message asks the caller to do. Only "pong" needs IO,
# which is why the decision is separated from the transport at all: the async
# and sync receive loops differ in nothing else. Reading the message off the
# wire stays in each loop — `receive_json` is typed as Any, so validating it
# there is what keeps that Any from leaking into the shared code.
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
            raise GraphQLResponseError([
                {"message": f"Expected connection_ack, got {message}"}
            ])


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
                raise malformed_next_error(exc) from exc
            return Emit(next_result(payload, result_type, slot_readers))
        case "error":
            try:
                error_payload = models.ws_error.model_validate({
                    "errors": message.payload
                }).errors
            except pydantic.ValidationError as exc:
                raise GraphQLResponseError([
                    {"message": f"Malformed error payload: {exc}"}
                ]) from exc
            raise GraphQLResponseError(
                error_payload or [{"message": f"Error without payload: {message}"}]
            )
        case "complete":
            return "stop"
        case "ping":
            return "pong"
        case "pong":
            return "skip"
        case _:
            detail = f"type: {message.type!r}, message: {message}"
            msg = f"Unexpected subscription message {detail}"
            raise GraphQLResponseError([{"message": msg}])


def _disconnect_error(exc: WebSocketDisconnect, prefix: str) -> GraphQLResponseError:
    msg = f"{prefix} with code {exc.code}"
    if exc.reason:
        msg = f"{msg}: {exc.reason}"
    return GraphQLResponseError([{"message": msg}])


def _invalid_json_error(exc: json.JSONDecodeError) -> GraphQLResponseError:
    return GraphQLResponseError([{"message": f"Server sent invalid JSON: {exc}"}])


@asynccontextmanager
async def async_graphql_ws_subscribe[T: pydantic.BaseModel](
    ws: AsyncWebSocketSession,
    result_type: type[T],
    payload: dict[str, Any],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> AsyncGenerator[AsyncGenerator[T]]:
    await _async_ws_handshake(ws, models)
    await ws.send_json({"id": "1", "type": "subscribe", "payload": payload})
    yield _async_ws_receive_messages(ws, result_type, slot_readers, models)


async def _async_ws_handshake(
    ws: AsyncWebSocketSession, models: ProtocolModels
) -> None:
    await ws.send_json({"type": "connection_init"})
    for _ in range(_MAX_PRE_ACK_MESSAGES):
        try:
            message = models.ws_message.model_validate(
                await asyncio.wait_for(
                    ws.receive_json(), timeout=_WS_CONNECTION_ACK_TIMEOUT_SECONDS
                )
            )
        except WebSocketDisconnect as exc:
            raise _disconnect_error(
                exc, "WebSocket disconnected before connection_ack"
            ) from exc
        except json.JSONDecodeError as exc:
            raise GraphQLResponseError([
                {"message": f"Server sent invalid JSON during handshake: {exc}"}
            ]) from exc
        except TimeoutError as exc:
            timeout = _WS_CONNECTION_ACK_TIMEOUT_SECONDS
            msg = f"Timed out waiting for connection_ack after {timeout} seconds"
            raise GraphQLResponseError([{"message": msg}]) from exc
        except pydantic.ValidationError as exc:
            raise GraphQLResponseError([
                {"message": f"Malformed protocol message during handshake: {exc}"}
            ]) from exc
        match _handshake_action(message):
            case "ack":
                return
            case "pong":
                await ws.send_json({"type": "pong"})
            case "skip":
                pass
    limit = _MAX_PRE_ACK_MESSAGES
    msg = f"No connection_ack after {limit} messages"
    raise GraphQLResponseError([{"message": msg}])


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


async def _async_ws_receive_messages[T: pydantic.BaseModel](
    ws: AsyncWebSocketSession,
    result_type: type[T],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> AsyncGenerator[T]:
    while True:
        try:
            message = models.ws_message.model_validate(await ws.receive_json())
        except WebSocketDisconnect as exc:
            if exc.code != _WS_NORMAL_CLOSURE:
                raise _disconnect_error(exc, "WebSocket disconnected") from exc
            return
        except json.JSONDecodeError as exc:
            raise _invalid_json_error(exc) from exc
        except pydantic.ValidationError as exc:
            raise GraphQLResponseError([
                {"message": f"Malformed protocol message: {exc}"}
            ]) from exc
        match _message_action(message, result_type, slot_readers, models):
            case Emit(value=value):
                yield value
            case "pong":
                await ws.send_json({"type": "pong"})
            case "skip":
                pass
            case "stop":
                return


@contextmanager
def graphql_ws_subscribe[T: pydantic.BaseModel](
    ws: WebSocketSession,
    result_type: type[T],
    payload: dict[str, Any],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> Generator[Generator[T]]:
    _ws_handshake(ws, models)
    ws.send_json({"id": "1", "type": "subscribe", "payload": payload})
    yield _ws_receive_messages(ws, result_type, slot_readers, models)


def _ws_handshake(ws: WebSocketSession, models: ProtocolModels) -> None:
    ws.send_json({"type": "connection_init"})
    for _ in range(_MAX_PRE_ACK_MESSAGES):
        try:
            message = models.ws_message.model_validate(
                ws.receive_json(timeout=_WS_CONNECTION_ACK_TIMEOUT_SECONDS)
            )
        except WebSocketDisconnect as exc:
            raise _disconnect_error(
                exc, "WebSocket disconnected before connection_ack"
            ) from exc
        except json.JSONDecodeError as exc:
            raise GraphQLResponseError([
                {"message": f"Server sent invalid JSON during handshake: {exc}"}
            ]) from exc
        except TimeoutError as exc:
            timeout = _WS_CONNECTION_ACK_TIMEOUT_SECONDS
            msg = f"Timed out waiting for connection_ack after {timeout} seconds"
            raise GraphQLResponseError([{"message": msg}]) from exc
        except pydantic.ValidationError as exc:
            raise GraphQLResponseError([
                {"message": f"Malformed protocol message during handshake: {exc}"}
            ]) from exc
        match _handshake_action(message):
            case "ack":
                return
            case "pong":
                ws.send_json({"type": "pong"})
            case "skip":
                pass
    limit = _MAX_PRE_ACK_MESSAGES
    msg = f"No connection_ack after {limit} messages"
    raise GraphQLResponseError([{"message": msg}])


def _ws_receive_messages[T: pydantic.BaseModel](
    ws: WebSocketSession,
    result_type: type[T],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> Generator[T]:
    while True:
        try:
            message = models.ws_message.model_validate(ws.receive_json())
        except WebSocketDisconnect as exc:
            if exc.code != _WS_NORMAL_CLOSURE:
                raise _disconnect_error(exc, "WebSocket disconnected") from exc
            return
        except json.JSONDecodeError as exc:
            raise _invalid_json_error(exc) from exc
        except pydantic.ValidationError as exc:
            raise GraphQLResponseError([
                {"message": f"Malformed protocol message: {exc}"}
            ]) from exc
        match _message_action(message, result_type, slot_readers, models):
            case Emit(value=value):
                yield value
            case "pong":
                ws.send_json({"type": "pong"})
            case "skip":
                pass
            case "stop":
                return
