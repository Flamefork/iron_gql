from typing import Any


class GraphQLResponseError(Exception):
    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        messages = "; ".join(e.get("message", str(e)) for e in errors)
        super().__init__(messages)


# A subscription stream ended before the server confirmed the operation with
# `complete`: the response closed early or the connection broke mid-read. It
# says the operation was not seen to finish, not that reopening it recovers
# what was missed -- that is the server's promise to make.
class SubscriptionInterruptedError(Exception):
    pass
