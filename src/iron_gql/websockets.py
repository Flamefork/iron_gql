import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pydantic
from httpx_ws import AsyncWebSocketSession
from httpx_ws import WebSocketDisconnect

from iron_gql.errors import GraphQLResponseError


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


@asynccontextmanager
async def graphql_ws_subscribe[T: pydantic.BaseModel](
    ws: AsyncWebSocketSession,
    result_type: type[T],
    query: str,
    variables: dict[str, Any] | None = None,
) -> AsyncGenerator[AsyncGenerator[T]]:
    await _ws_handshake(ws)
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    await ws.send_json({"id": "1", "type": "subscribe", "payload": payload})
    yield _ws_receive_messages(ws, result_type)


async def _ws_handshake(ws: AsyncWebSocketSession) -> None:
    await ws.send_json({"type": "connection_init"})
    for _ in range(_MAX_PRE_ACK_MESSAGES):
        try:
            message = _WSMessage.model_validate(
                await asyncio.wait_for(
                    ws.receive_json(), timeout=_WS_CONNECTION_ACK_TIMEOUT_SECONDS
                )
            )
        except WebSocketDisconnect as exc:
            msg = f"WebSocket disconnected before connection_ack with code {exc.code}"
            if exc.reason:
                msg = f"{msg}: {exc.reason}"
            raise GraphQLResponseError([{"message": msg}]) from exc
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
        match message.type:
            case "connection_ack":
                return
            case "ping":
                await ws.send_json({"type": "pong"})
            case "pong":
                pass
            case _:
                raise GraphQLResponseError([
                    {"message": f"Expected connection_ack, got {message}"}
                ])
    limit = _MAX_PRE_ACK_MESSAGES
    msg = f"No connection_ack after {limit} messages"
    raise GraphQLResponseError([{"message": msg}])


def ws_url(url: httpx.URL) -> httpx.URL:
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


async def _ws_receive_messages[T: pydantic.BaseModel](  # noqa: C901, PLR0912
    ws: AsyncWebSocketSession,
    result_type: type[T],
) -> AsyncGenerator[T]:
    while True:
        try:
            message = _WSMessage.model_validate(await ws.receive_json())
        except WebSocketDisconnect as exc:
            if exc.code != _WS_NORMAL_CLOSURE:
                msg = f"WebSocket disconnected with code {exc.code}"
                if exc.reason:
                    msg = f"{msg}: {exc.reason}"
                raise GraphQLResponseError([{"message": msg}]) from exc
            return
        except json.JSONDecodeError as exc:
            raise GraphQLResponseError([
                {"message": f"Server sent invalid JSON: {exc}"}
            ]) from exc
        except pydantic.ValidationError as exc:
            raise GraphQLResponseError([
                {"message": f"Malformed protocol message: {exc}"}
            ]) from exc
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
                # Same contract as GQLClient.query: a payload that fails
                # result validation surfaces as pydantic.ValidationError with
                # a response path, identically across both transports.
                yield result_type.model_validate(payload.data)
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
                return
            case "ping":
                await ws.send_json({"type": "pong"})
            case "pong":
                pass
            case _:
                detail = f"type: {message.type!r}, message: {message}"
                msg = f"Unexpected subscription message {detail}"
                raise GraphQLResponseError([{"message": msg}])
