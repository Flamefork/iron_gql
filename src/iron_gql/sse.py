from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from collections.abc import Generator
from collections.abc import Iterator
from contextlib import aclosing
from contextlib import asynccontextmanager
from contextlib import closing
from contextlib import contextmanager
from typing import Any
from typing import Literal
from typing import Never

import httpx2
import pydantic

from iron_gql.errors import GraphQLResponseError
from iron_gql.errors import SubscriptionInterruptedError
from iron_gql.response import raise_for_status
from iron_gql.response import response_body
from iron_gql.slots import SlotReaders
from iron_gql.subscription import NEXT_PAYLOAD
from iron_gql.subscription import Emit
from iron_gql.subscription import malformed_next_error
from iron_gql.subscription import next_result

_EVENT_STREAM = "text/event-stream"

# The transport failures that end an accepted stream before its `complete`:
# the peer closed mid-body, the connection broke under a read, or nothing
# arrived within the idle timeout. Anything else raised while reading -- a
# malformed event, a result that fails validation, an oversized event
# (`httpx2.SSEError`, a `TransportError` too) -- is not an interruption and
# passes through as itself.
_INTERRUPTIONS = (httpx2.RemoteProtocolError, httpx2.ReadError, httpx2.ReadTimeout)

type EventAction[T: pydantic.BaseModel] = Emit[T] | Literal["stop"]


def _event_action[T: pydantic.BaseModel](
    event: httpx2.ServerSentEvent,
    result_type: type[T],
    slot_readers: SlotReaders | None,
) -> EventAction[T]:
    match event.event:
        case "next":
            try:
                payload = NEXT_PAYLOAD.validate_json(event.data)
            except pydantic.ValidationError as exc:
                raise malformed_next_error(exc) from exc
            return Emit(next_result(payload, result_type, slot_readers))
        case "complete":
            return "stop"
        case _:
            detail = f"event: {event.event!r}, data: {event.data!r}"
            msg = f"Unexpected subscription event {detail}"
            raise GraphQLResponseError([{"message": msg}])


def _content_type(response: httpx2.Response) -> str:
    return response.headers.get("Content-Type", "")


def _is_event_stream(response: httpx2.Response) -> bool:
    return _content_type(response).partition(";")[0].strip().lower() == _EVENT_STREAM


# A response that opened no stream: a refusal before the stream, by status,
# or a single GraphQL response in its place. Read the way a query's response
# is, so a refusal carries the same error a query's would.
def _reject(response: httpx2.Response) -> Never:
    raise_for_status(response)
    response_body(response)
    msg = f"Expected an event stream, got {_content_type(response)!r}"
    raise GraphQLResponseError([{"message": msg}])


def _ended_without_complete() -> SubscriptionInterruptedError:
    return SubscriptionInterruptedError("Event stream ended without complete")


def _broke_before_complete(exc: httpx2.TransportError) -> SubscriptionInterruptedError:
    return SubscriptionInterruptedError(f"Event stream broke before complete: {exc}")


@asynccontextmanager
async def async_graphql_sse_subscribe[T: pydantic.BaseModel](
    client: httpx2.AsyncClient,
    url: httpx2.URL,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeouts: httpx2.Timeout,
    result_type: type[T],
    slot_readers: SlotReaders | None,
) -> AsyncGenerator[AsyncGenerator[T]]:
    async with client.sse(
        url, method="POST", json=payload, headers=headers, timeout=timeouts
    ) as source:
        response = source.response
        if not (response.is_success and _is_event_stream(response)):
            await response.aread()
            _reject(response)
        async with aclosing(
            _async_receive_events(aiter(source), result_type, slot_readers)
        ) as stream:
            yield stream


async def _async_receive_events[T: pydantic.BaseModel](
    events: AsyncIterator[httpx2.ServerSentEvent],
    result_type: type[T],
    slot_readers: SlotReaders | None,
) -> AsyncGenerator[T]:
    while True:
        try:
            event = await anext(events)
        except StopAsyncIteration:
            raise _ended_without_complete() from None
        except _INTERRUPTIONS as exc:
            raise _broke_before_complete(exc) from exc
        match _event_action(event, result_type, slot_readers):
            case Emit(value=value):
                yield value
            case "stop":
                return


@contextmanager
def graphql_sse_subscribe[T: pydantic.BaseModel](
    client: httpx2.Client,
    url: httpx2.URL,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeouts: httpx2.Timeout,
    result_type: type[T],
    slot_readers: SlotReaders | None,
) -> Generator[Generator[T]]:
    with client.sse(
        url, method="POST", json=payload, headers=headers, timeout=timeouts
    ) as source:
        response = source.response
        if not (response.is_success and _is_event_stream(response)):
            response.read()
            _reject(response)
        with closing(
            _receive_events(iter(source), result_type, slot_readers)
        ) as stream:
            yield stream


def _receive_events[T: pydantic.BaseModel](
    events: Iterator[httpx2.ServerSentEvent],
    result_type: type[T],
    slot_readers: SlotReaders | None,
) -> Generator[T]:
    while True:
        try:
            event = next(events)
        except StopIteration:
            raise _ended_without_complete() from None
        except _INTERRUPTIONS as exc:
            raise _broke_before_complete(exc) from exc
        match _event_action(event, result_type, slot_readers):
            case Emit(value=value):
                yield value
            case "stop":
                return
