from dataclasses import dataclass

import pydantic

from iron_gql.errors import GraphQLResponseError
from iron_gql.errors import ResponseProtocolError
from iron_gql.protocol import NextPayload
from iron_gql.slots import SlotReaders


@dataclass(frozen=True, slots=True)
class Emit[T: pydantic.BaseModel]:
    value: T


# `status_code` is that of the response that opened the stream, `None` over a
# WebSocket.
def malformed_next_error(
    exc: pydantic.ValidationError, status_code: int | None
) -> ResponseProtocolError:
    msg = f"Malformed next payload: {exc}"
    return ResponseProtocolError(msg, status_code=status_code)


# The result one `next` carries, whichever protocol delivered it. Errors end
# the stream: a subscription has no partial result to hand over.
def next_result[T: pydantic.BaseModel](
    payload: NextPayload,
    result_type: type[T],
    slot_readers: SlotReaders | None,
    status_code: int | None,
) -> T:
    if payload.errors:
        raise GraphQLResponseError(payload.errors, status_code=status_code)
    if payload.data is None:
        msg = "No data in next payload"
        raise ResponseProtocolError(msg, status_code=status_code)
    # Same contract as the query path: a payload that fails result validation
    # surfaces as pydantic.ValidationError with a response path, identically
    # across both transports.
    return result_type.model_validate(payload.data, context=slot_readers)
