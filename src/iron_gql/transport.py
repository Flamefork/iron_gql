from collections.abc import Generator
from contextlib import contextmanager

import httpx2
from httpx2.websockets import HTTPXWSException
from httpx2.websockets import WebSocketDisconnect
from httpx2.websockets import WebSocketInvalidTypeReceived
from httpx2.websockets import WebSocketNetworkError
from httpx2.websockets import WebSocketUpgradeError

from iron_gql.errors import HTTPResponseError
from iron_gql.errors import ResponseProtocolError
from iron_gql.errors import TransportError
from iron_gql.errors import WebSocketClosedError

# Close codes a protocol over WebSocket gives its own meaning (RFC 6455,
# 7.4.2): graphql-transport-ws refuses with them.
_REFUSAL_CLOSE_CODES = range(4000, 5000)
# httpx2 closes with it on a message over its own limit: opening the stream
# again would meet the same message.
_MESSAGE_TOO_BIG = 1009

_UNSUPPORTED_URL_HINT = (
    "only an http:// or https:// URL can be requested: correct base_url, "
    "or the redirect that led elsewhere"
)
_INVALID_REQUEST_HINT = (
    "the request is not valid HTTP: correct the headers given to the client "
    "and to the call"
)


# The failures of `httpx2` in an operation, of its HTTP side and its websockets
# alike, as the client's own (ADR 0009); one this table does not name passes
# as it is. Wrapped around the client's own steps and never around a `yield`
# to the caller: what the body of a `with` raises is not a failure of the
# operation.
#
# `broken` is the type of a broken connection, which depends on whether the
# subscription was accepted; `status_code` is that of the response being read.
@contextmanager
def translated(
    action: str,
    *,
    broken: type[TransportError] = TransportError,
    status_code: int | None = None,
) -> Generator[None]:
    try:
        yield
    except (httpx2.RequestError, HTTPXWSException) as exc:
        own = own_error(exc, action, broken=broken, status_code=status_code)
        if own is None:
            raise
        raise own from exc


def own_error(
    exc: BaseException,
    action: str,
    *,
    broken: type[TransportError],
    status_code: int | None,
) -> Exception | None:
    match exc:
        case (
            httpx2.TimeoutException()
            | httpx2.NetworkError()
            | httpx2.RemoteProtocolError()
            | httpx2.ProxyError()
        ):
            return broken(_failed(action, exc))
        case httpx2.DecodingError() | httpx2.SSEError() | httpx2.TooManyRedirects():
            return ResponseProtocolError(_failed(action, exc), status_code=status_code)
        case httpx2.UnsupportedProtocol():
            return ValueError(f"{_failed(action, exc)}; {_UNSUPPORTED_URL_HINT}")
        case httpx2.LocalProtocolError():
            return ValueError(f"{_failed(action, exc)}; {_INVALID_REQUEST_HINT}")
        case WebSocketUpgradeError():
            return HTTPResponseError(
                f"The WebSocket was refused: {response_summary(exc.response)}",
                response=exc.response,
            )
        case WebSocketDisconnect():
            return _closed(action, exc, broken)
        case WebSocketNetworkError():
            return broken(f"{action} failed: the connection broke")
        case WebSocketInvalidTypeReceived():
            return ResponseProtocolError(
                f"Expected a text message, got {type(exc.event).__name__}",
                status_code=None,
            )
        case _:
            return None


# Reads the body of a response whose head has arrived. A refusal stays the
# refusal its status says when its body cannot be read.
@contextmanager
def reading_body(response: httpx2.Response) -> Generator[None]:
    action = "Reading the response body"
    try:
        with translated(action, status_code=response.status_code):
            yield
    except (TransportError, ResponseProtocolError) as exc:
        if response.is_success:
            raise
        raise HTTPResponseError(
            response_summary(response), response=response
        ) from exc.__cause__


def response_summary(response: httpx2.Response) -> str:
    status = response.status_code
    if not httpx2.codes.is_redirect(status):
        return f"Unexpected response status {status} {response.reason_phrase}".rstrip()
    location: str = response.headers.get("Location", "")
    message = f"Unexpected 3xx response ({status})"
    return f"{message} to {location}" if location else message


def _failed(action: str, exc: httpx2.RequestError) -> str:
    # `httpx2` leaves the text of some of its exceptions empty.
    text = str(exc)
    name = type(exc).__name__
    return f"{action} failed: {name}: {text}" if text else f"{action} failed: {name}"


def _closed(
    action: str, exc: WebSocketDisconnect, broken: type[TransportError]
) -> Exception:
    code = int(exc.code)
    message = f"{action} failed: the WebSocket closed with code {code}"
    if exc.reason:
        message = f"{message}: {exc.reason}"
    if code in _REFUSAL_CLOSE_CODES:
        return WebSocketClosedError(message, close_code=code, reason=exc.reason)
    if code == _MESSAGE_TOO_BIG:
        return ResponseProtocolError(message, status_code=None)
    return broken(message)
