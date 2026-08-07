import asyncio
import json
from collections.abc import AsyncGenerator
from collections.abc import Generator
from contextlib import asynccontextmanager
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from typing import Literal

import httpx2
import pydantic
from httpx2.websockets import AsyncWebSocketSession
from httpx2.websockets import WebSocketDisconnect
from httpx2.websockets import WebSocketSession

from iron_gql.errors import GraphQLResponseError
from iron_gql.slots import SlotHandles


class _WSMessage(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore")
    type: str | None = None
    payload: object = None


class _NextPayload(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore")
    data: dict[str, Any] | None = None
    errors: list[dict[str, Any]] | None = None


_NEXT_PAYLOAD = pydantic.TypeAdapter(_NextPayload)
_ERROR_PAYLOAD: pydantic.TypeAdapter[list[dict[str, Any]] | None] = (
    pydantic.TypeAdapter(list[dict[str, Any]] | None)
)


_MAX_PRE_ACK_MESSAGES = 16
_WS_CONNECTION_ACK_TIMEOUT_SECONDS = 10
_WS_NORMAL_CLOSURE = 1000


@dataclass(frozen=True, slots=True)
class Emit[T: pydantic.BaseModel]:
    value: T


# What a received protocol message asks the caller to do. Only "pong" needs IO,
# which is why the decision is separated from the transport at all: the async
# and sync receive loops differ in nothing else. Reading the message off the
# wire stays in each loop — `receive_json` is typed as Any, so validating it
# there is what keeps that Any from leaking into the shared code.
type MessageAction[T: pydantic.BaseModel] = Emit[T] | Literal["pong", "skip", "stop"]


def _handshake_action(message: _WSMessage) -> Literal["ack", "pong", "skip"]:
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
    message: _WSMessage,
    result_type: type[T],
    slot_handles: SlotHandles | None,
) -> MessageAction[T]:
    match message.type:
        case "next":
            try:
                payload = _NEXT_PAYLOAD.validate_python(message.payload or {})
            except pydantic.ValidationError as exc:
                raise GraphQLResponseError([
                    {"message": f"Malformed next payload: {exc}"}
                ]) from exc
            if payload.errors or payload.data is None:
                raise GraphQLResponseError(
                    payload.errors or [{"message": "No data in response"}]
                )
            # Same contract as the query path: a payload that fails result
            # validation surfaces as pydantic.ValidationError with a response
            # path, identically across both transports.
            return Emit(result_type.model_validate(payload.data, context=slot_handles))
        case "error":
            try:
                error_payload = _ERROR_PAYLOAD.validate_python(message.payload)
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
    query: str,
    variables: dict[str, Any] | None,
    slot_handles: SlotHandles | None,
) -> AsyncGenerator[AsyncGenerator[T]]:
    await _async_ws_handshake(ws)
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    await ws.send_json({"id": "1", "type": "subscribe", "payload": payload})
    yield _async_ws_receive_messages(ws, result_type, slot_handles)


async def _async_ws_handshake(ws: AsyncWebSocketSession) -> None:
    await ws.send_json({"type": "connection_init"})
    for _ in range(_MAX_PRE_ACK_MESSAGES):
        try:
            message = _WSMessage.model_validate(
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
    slot_handles: SlotHandles | None,
) -> AsyncGenerator[T]:
    while True:
        try:
            message = _WSMessage.model_validate(await ws.receive_json())
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
        match _message_action(message, result_type, slot_handles):
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
    query: str,
    variables: dict[str, Any] | None,
    slot_handles: SlotHandles | None,
) -> Generator[Generator[T]]:
    _ws_handshake(ws)
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    ws.send_json({"id": "1", "type": "subscribe", "payload": payload})
    yield _ws_receive_messages(ws, result_type, slot_handles)


def _ws_handshake(ws: WebSocketSession) -> None:
    ws.send_json({"type": "connection_init"})
    for _ in range(_MAX_PRE_ACK_MESSAGES):
        try:
            message = _WSMessage.model_validate(
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
    slot_handles: SlotHandles | None,
) -> Generator[T]:
    while True:
        try:
            message = _WSMessage.model_validate(ws.receive_json())
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
        match _message_action(message, result_type, slot_handles):
            case Emit(value=value):
                yield value
            case "pong":
                ws.send_json({"type": "pong"})
            case "skip":
                pass
            case "stop":
                return
