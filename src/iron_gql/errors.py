from typing import Any


class GraphQLResponseError(Exception):
    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        messages = "; ".join(e.get("message", str(e)) for e in errors)
        super().__init__(messages)
