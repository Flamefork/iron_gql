import functools
from dataclasses import dataclass
from types import new_class
from typing import Any
from typing import cast

import pydantic

from iron_gql.errors import GraphQLErrors

# The models the runtime reads the GraphQL protocol with: a response body, a
# subscription's `next` payload, a graphql-transport-ws message and the payload
# of its `error`. The classes here are their shapes; the client parses on
# `protocol_models(base)`, the same shapes derived from the consumer's base, so
# a setting of that base -- hiding the input in errors -- holds for them too.
#
# Each shape pins what the protocol fixes: the wire keys (no alias generator)
# and the extra keys a message may carry (ignored). Those keys win over the
# base's; every other key of the base applies.


class ResponseBody(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore", alias_generator=None)
    data: dict[str, Any] | None = None
    errors: GraphQLErrors | None = None


class NextPayload(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore", alias_generator=None)
    data: dict[str, Any] | None = None
    errors: GraphQLErrors | None = None


class WSMessage(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore", alias_generator=None)
    type: str | None = None
    payload: object = None


# The payload of a graphql-transport-ws `error` is the list itself; it is read
# as `{"errors": payload}`, so the list is validated by a model like every
# other message.
class WSErrorPayload(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore", alias_generator=None)
    errors: GraphQLErrors | None = None


@dataclass(frozen=True, slots=True)
class ProtocolModels:
    body: type[ResponseBody]
    next_payload: type[NextPayload]
    ws_message: type[WSMessage]
    ws_error: type[WSErrorPayload]


# The shapes derived from `base`, built once per base. The shape comes first
# among the bases so the class is consistent whatever the base, plain
# `pydantic.BaseModel` included; its config is restated in the class body,
# which pydantic ranks over every base's.
@functools.cache
def protocol_models(base: type[pydantic.BaseModel]) -> ProtocolModels:
    return ProtocolModels(
        body=_on_base(ResponseBody, base),
        next_payload=_on_base(NextPayload, base),
        ws_message=_on_base(WSMessage, base),
        ws_error=_on_base(WSErrorPayload, base),
    )


def _on_base[M: pydantic.BaseModel](
    shape: type[M], base: type[pydantic.BaseModel]
) -> type[M]:
    def body(namespace: dict[str, Any]) -> None:
        namespace["__module__"] = shape.__module__
        namespace["model_config"] = shape.model_config.copy()

    # `new_class` returns a bare `type`; the class derives from `shape`.
    return cast("type[M]", new_class(shape.__name__, (shape, base), exec_body=body))
