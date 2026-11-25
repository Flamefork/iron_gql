from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import NotRequired
from typing import TypedDict
from typing import cast

import httpx2


# How long one call waits at each of its steps (ADR 0010); `None` waits without
# a limit.
@dataclass(frozen=True, slots=True, kw_only=True)
class CallLimits:
    connect_timeout: float | None
    timeout: float | None
    stream_idle_timeout: float | None


# The arguments of one request of `httpx2` beside its method, URL and content.
class RequestOptions(TypedDict):
    headers: dict[str, str]
    extensions: Mapping[str, Any]
    auth: NotRequired[
        httpx2.Auth
        | tuple[str | bytes, str | bytes]
        | Callable[[httpx2.Request], httpx2.Request]
        | None
    ]
    follow_redirects: NotRequired[bool]


# One call of an operation, with the options of the operation and its client
# resolved. `httpx_options` is the operation's: its keys are checked, its values
# are `httpx2`'s to judge.
class Call:
    def __init__(
        self,
        limits: CallLimits,
        *,
        headers: dict[str, str],
        httpx_options: Mapping[str, Any],
    ):
        self.limits = limits
        self._phases = httpx2.Timeout(
            limits.connect_timeout, read=limits.timeout
        ).as_dict()
        extensions = cast("Mapping[str, Any] | None", httpx_options.get("extensions"))
        # The limits go out as the extension that `httpx2` makes of its `timeout`
        # argument, a dict this call holds: `stream_accepted` changes it and never
        # one that an auth or a hook of the consumer put on the request.
        request = {
            **httpx_options,
            "headers": headers,
            "extensions": {
                **({} if extensions is None else extensions),
                "timeout": self._phases,
            },
        }
        self.request = cast("RequestOptions", cast("object", request))

    # From here on it is the silence of an SSE stream that is limited. `httpx2`
    # has one read timeout for the head of a response and for its body, and reads
    # it from the extension again as the body begins. It hands the same dict on
    # to a request that follows a redirect or goes through a proxy.
    def stream_accepted(self) -> None:
        self._phases["read"] = self.limits.stream_idle_timeout
