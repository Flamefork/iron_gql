import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any

import httpx
import pydantic
from anyio import ClosedResourceError
from httpx_ws import AsyncWebSocketSession
from httpx_ws import WebSocketDisconnect
from httpx_ws.transport import ASGIWebSocketTransport

from iron_gql.errors import GraphQLResponseError

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
            message = await asyncio.wait_for(
                ws.receive_json(), timeout=_WS_CONNECTION_ACK_TIMEOUT_SECONDS
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
        match message.get("type"):
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
            message = await ws.receive_json()
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
        match message.get("type"):
            case "next":
                payload = message.get("payload") or {}
                errors = payload.get("errors")
                data = payload.get("data")
                if errors or data is None:
                    raise GraphQLResponseError(
                        errors or [{"message": "No data in response"}]
                    )
                try:
                    yield result_type.model_validate(data)
                except pydantic.ValidationError as exc:
                    raise GraphQLResponseError([
                        {"message": f"Invalid data in response: {exc}"}
                    ]) from exc
            case "error":
                raise GraphQLResponseError(
                    message.get("payload")
                    or [{"message": f"Error without payload: {message}"}]
                )
            case "complete":
                return
            case "ping":
                await ws.send_json({"type": "pong"})
            case "pong":
                pass
            case _:
                msg = (
                    "Unexpected subscription message"
                    f" type: {message.get('type')!r},"
                    f" message: {message}"
                )
                raise GraphQLResponseError([{"message": msg}])


# See https://github.com/frankie567/httpx-ws/issues/136
class SafeASGIWebSocketTransport(ASGIWebSocketTransport):
    async def _handle_ws_request(
        self, request: httpx.Request, scope: dict[str, Any]
    ) -> httpx.Response:
        response = await super()._handle_ws_request(request, scope)
        self._ws_network_stream = response.extensions["network_stream"]
        return response

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: TracebackType | None = None,
    ) -> None:
        try:
            await super().__aexit__(exc_type, exc_val, exc_tb)
        except BaseExceptionGroup as eg:
            if exc_val is not None:
                _, rest = eg.split(lambda e: e is exc_val)
                if rest is not None:
                    raise rest from exc_val
                raise exc_val  # noqa: B904
            raise
        finally:
            stream = getattr(self, "_ws_network_stream", None)
            if stream is not None:
                with contextlib.suppress(ClosedResourceError):
                    await stream._receive_queue.aclose()  # noqa: SLF001
                with contextlib.suppress(ClosedResourceError):
                    await stream._send_queue.aclose()  # noqa: SLF001
