from dataclasses import dataclass

import pydantic

from iron_gql.errors import GraphQLResponseError
from iron_gql.protocol import NextPayload
from iron_gql.slots import SlotReaders


@dataclass(frozen=True, slots=True)
class Emit[T: pydantic.BaseModel]:
    value: T


def malformed_next_error(exc: pydantic.ValidationError) -> GraphQLResponseError:
    return GraphQLResponseError([{"message": f"Malformed next payload: {exc}"}])


# The result one `next` carries, whichever protocol delivered it. Errors end
# the stream: a subscription has no partial result to hand over.
def next_result[T: pydantic.BaseModel](
    payload: NextPayload,
    result_type: type[T],
    slot_readers: SlotReaders | None,
) -> T:
    if payload.errors or payload.data is None:
        raise GraphQLResponseError(
            payload.errors or [{"message": "No data in response"}]
        )
    # Same contract as the query path: a payload that fails result validation
    # surfaces as pydantic.ValidationError with a response path, identically
    # across both transports.
    return result_type.model_validate(payload.data, context=slot_readers)
