from typing import Annotated
from typing import Any

import pydantic


class GraphQLResponseError(Exception):
    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        messages = "; ".join(e.get("message", str(e)) for e in errors)
        super().__init__(messages)


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


# A subscription stream ended before the server confirmed the operation with
# `complete`: the response closed early or the connection broke mid-read. It
# says the operation was not seen to finish, not that reopening it recovers
# what was missed -- that is the server's promise to make.
class SubscriptionInterruptedError(Exception):
    pass
