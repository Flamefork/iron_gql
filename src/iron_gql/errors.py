from typing import Annotated
from typing import Any
from typing import cast
from typing import override

import httpx2
import pydantic

# The errors of a client's operation (ADR 0009). Each raise names its
# `status_code`, `None` included, so that no answer loses its status on the way.


class GraphQLClientError(Exception):
    pass


# The server's answer did not arrive whole.
class TransportError(GraphQLClientError):
    pass


# A subscription stream ended before the server confirmed the operation with
# `complete`: the response closed early or the connection broke mid-read. It
# says the operation was not seen to finish, not that reopening it recovers
# what was missed -- that is the server's promise to make.
class SubscriptionInterruptedError(TransportError):
    pass


# The server answered with something other than a result. `status_code` is the
# status of the HTTP response that carried the answer -- for an event, of the
# response that opened the stream -- and `None` when it came over a WebSocket
# or the status is not known to the client.
class ResponseError(GraphQLClientError):
    def __init__(self, message: str, *, status_code: int | None):
        super().__init__(message)
        self._status_code = status_code

    @property
    def status_code(self) -> int | None:
        return self._status_code


# `errors` are the server's own: as many, in the order and as they came.
class GraphQLResponseError(ResponseError):
    def __init__(self, errors: list[dict[str, Any]], *, status_code: int | None):
        messages = "; ".join(e["message"] or str(e) for e in errors)
        super().__init__(messages, status_code=status_code)
        self.errors = errors


class HTTPResponseError(ResponseError):
    def __init__(self, message: str, *, response: httpx2.Response):
        super().__init__(message, status_code=response.status_code)
        self.response = response

    @property
    @override
    def status_code(self) -> int:
        # Set from the response, which always has one.
        return cast("int", super().status_code)


class ResponseProtocolError(ResponseError):
    pass


class WebSocketClosedError(ResponseError):
    def __init__(self, message: str, *, close_code: int, reason: str):
        super().__init__(message, status_code=None)
        self.close_code = close_code
        self.reason = reason


# The `errors` a server sends, in a response body or a subscription message.
# GraphQL gives every error a string `message`: an error without one is not a
# GraphQL error, and the payload that carries it is malformed.
def _graphql_error(error: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(error.get("message"), str):
        msg = "a GraphQL error carries a string `message`"
        raise ValueError(msg)  # noqa: TRY004 -- pydantic reports ValueError only
    return error


type GraphQLErrors = list[
    Annotated[dict[str, Any], pydantic.AfterValidator(_graphql_error)]
]
