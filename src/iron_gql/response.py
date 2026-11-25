from typing import Any

import httpx2
import pydantic

from iron_gql.errors import GraphQLResponseError
from iron_gql.errors import HTTPResponseError
from iron_gql.errors import ResponseProtocolError
from iron_gql.protocol import ProtocolModels
from iron_gql.protocol import ResponseBody
from iron_gql.transport import response_summary

GRAPHQL_RESPONSE = "application/graphql-response+json"


# The media type of the response, without parameters and in lower case: how
# GraphQL over HTTP compares it.
def media_type(response: httpx2.Response) -> str:
    content_type: str = response.headers.get("Content-Type", "")
    return content_type.partition(";")[0].strip().lower()


# A status that is not 2xx raises. Under GraphQL over HTTP a response of the
# GraphQL media type is read independent of its status, so a 4xx or 5xx one
# whose body carries `errors` raises those; any other raises the response with
# its body inside.
def raise_for_status(response: httpx2.Response, models: ProtocolModels) -> None:
    if response.is_success:
        return
    if response.is_error and media_type(response) == GRAPHQL_RESPONSE:
        errors = _error_status_errors(response, models)
        if errors:
            raise GraphQLResponseError(errors, status_code=response.status_code)
    raise HTTPResponseError(response_summary(response), response=response)


def _error_status_errors(
    response: httpx2.Response, models: ProtocolModels
) -> list[dict[str, Any]] | None:
    try:
        return models.body.model_validate_json(response.content).errors
    except pydantic.ValidationError:
        return None


# The GraphQL response a successful status carries. Its `errors` are raised
# here; what to make of `data` is the caller's.
def response_body(response: httpx2.Response, models: ProtocolModels) -> ResponseBody:
    try:
        body = models.body.model_validate_json(response.content)
    except pydantic.ValidationError as exc:
        msg = f"Malformed response body: {exc}"
        raise ResponseProtocolError(msg, status_code=response.status_code) from exc
    if body.errors:
        raise GraphQLResponseError(body.errors, status_code=response.status_code)
    return body
