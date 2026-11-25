from collections.abc import AsyncGenerator
from collections.abc import Generator
from contextlib import AbstractContextManager
from contextlib import AsyncExitStack
from contextlib import ExitStack
from contextlib import aclosing
from contextlib import asynccontextmanager
from contextlib import closing
from contextlib import contextmanager
from typing import Any
from typing import Literal
from typing import Never

import httpx2
import pydantic

from iron_gql.call import Call
from iron_gql.errors import ResponseProtocolError
from iron_gql.errors import SubscriptionInterruptedError
from iron_gql.protocol import ProtocolModels
from iron_gql.response import media_type
from iron_gql.response import raise_for_status
from iron_gql.response import response_body
from iron_gql.slots import SlotReaders
from iron_gql.subscription import Emit
from iron_gql.subscription import malformed_next_error
from iron_gql.subscription import next_result
from iron_gql.transport import reading_body
from iron_gql.transport import translated

_EVENT_STREAM = "text/event-stream"
_OPENING = "Opening the event stream"

type EventAction[T: pydantic.BaseModel] = Emit[T] | Literal["stop"]


def _event_action[T: pydantic.BaseModel](
    event: httpx2.ServerSentEvent,
    result_type: type[T],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
    status_code: int,
) -> EventAction[T]:
    match event.event:
        case "next":
            try:
                payload = models.next_payload.model_validate_json(event.data)
            except pydantic.ValidationError as exc:
                raise malformed_next_error(exc, status_code) from exc
            return Emit(next_result(payload, result_type, slot_readers, status_code))
        case "complete":
            return "stop"
        case _:
            detail = f"event: {event.event!r}, data: {event.data!r}"
            msg = f"Unexpected subscription event {detail}"
            raise ResponseProtocolError(msg, status_code=status_code)


def _content_type(response: httpx2.Response) -> str:
    return response.headers.get("Content-Type", "")


def _is_event_stream(response: httpx2.Response) -> bool:
    return media_type(response) == _EVENT_STREAM


# A response that opened no stream: a refusal before the stream, by status,
# or a single GraphQL response in its place. Read the way a query's response
# is, so a refusal carries the same error a query's would.
def _reject(response: httpx2.Response, models: ProtocolModels) -> Never:
    raise_for_status(response, models)
    response_body(response, models)
    msg = f"Expected an event stream, got {_content_type(response)!r}"
    raise ResponseProtocolError(msg, status_code=response.status_code)


# The stream is accepted: a connection that breaks under it interrupts the
# subscription. An event that cannot be read -- an oversized one -- does not.
def _reading_events(status_code: int) -> AbstractContextManager[None]:
    return translated(
        "Reading the event stream before complete",
        broken=SubscriptionInterruptedError,
        status_code=status_code,
    )


def _ended_without_complete() -> SubscriptionInterruptedError:
    return SubscriptionInterruptedError("Event stream ended without complete")


@asynccontextmanager
async def async_graphql_sse_subscribe[T: pydantic.BaseModel](
    client: httpx2.AsyncClient,
    url: httpx2.URL,
    payload: dict[str, Any],
    *,
    call: Call,
    result_type: type[T],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> AsyncGenerator[AsyncGenerator[T]]:
    async with AsyncExitStack() as stack:
        with translated(_OPENING):
            source = await stack.enter_async_context(
                client.sse(url, method="POST", json=payload, **call.request)
            )
        response = source.response
        if not (response.is_success and _is_event_stream(response)):
            with reading_body(response):
                await response.aread()
            _reject(response, models)
        call.stream_accepted()
        yield await stack.enter_async_context(
            aclosing(_async_receive_events(source, result_type, slot_readers, models))
        )


async def _async_receive_events[T: pydantic.BaseModel](
    source: httpx2.EventSource,
    result_type: type[T],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> AsyncGenerator[T]:
    events = aiter(source)
    status_code = source.response.status_code
    while True:
        with _reading_events(status_code):
            event = await anext(events, None)
        if event is None:
            raise _ended_without_complete()
        match _event_action(event, result_type, slot_readers, models, status_code):
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
    call: Call,
    result_type: type[T],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> Generator[Generator[T]]:
    with ExitStack() as stack:
        with translated(_OPENING):
            source = stack.enter_context(
                client.sse(url, method="POST", json=payload, **call.request)
            )
        response = source.response
        if not (response.is_success and _is_event_stream(response)):
            with reading_body(response):
                response.read()
            _reject(response, models)
        call.stream_accepted()
        yield stack.enter_context(
            closing(_receive_events(source, result_type, slot_readers, models))
        )


def _receive_events[T: pydantic.BaseModel](
    source: httpx2.EventSource,
    result_type: type[T],
    slot_readers: SlotReaders | None,
    models: ProtocolModels,
) -> Generator[T]:
    events = iter(source)
    status_code = source.response.status_code
    while True:
        with _reading_events(status_code):
            event = next(events, None)
        if event is None:
            raise _ended_without_complete()
        match _event_action(event, result_type, slot_readers, models, status_code):
            case Emit(value=value):
                yield value
            case "stop":
                return
