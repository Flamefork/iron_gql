from typing import Any

import httpx2
import pydantic

from iron_gql.errors import GraphQLErrors
from iron_gql.errors import GraphQLResponseError

GRAPHQL_RESPONSE = "application/graphql-response+json"


class ResponseBody(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore")
    data: dict[str, Any] | None = None
    errors: GraphQLErrors | None = None


# The media type of the response, without parameters and in lower case: how
# GraphQL over HTTP compares it.
def media_type(response: httpx2.Response) -> str:
    content_type: str = response.headers.get("Content-Type", "")
    return content_type.partition(";")[0].strip().lower()


# A status that is not 2xx raises. Under GraphQL over HTTP a response of the
# GraphQL media type is read independent of its status, so a 4xx or 5xx one
# whose body carries `errors` raises those; any other raises
# `httpx2.HTTPStatusError`, the response with its body inside.
def raise_for_status(response: httpx2.Response) -> None:
    if httpx2.codes.is_redirect(response.status_code):
        location: str = response.headers.get("Location", "")
        message = f"Unexpected 3xx response ({response.status_code})"
        if location:
            message = f"{message} to {location}"
        raise httpx2.HTTPStatusError(
            message, request=response.request, response=response
        )
    if response.is_error and media_type(response) == GRAPHQL_RESPONSE:
        errors = _error_status_errors(response)
        if errors:
            raise GraphQLResponseError(errors)
    response.raise_for_status()


def _error_status_errors(response: httpx2.Response) -> list[dict[str, Any]] | None:
    try:
        return ResponseBody.model_validate_json(response.content).errors
    except pydantic.ValidationError:
        return None


# The GraphQL response a successful status carries. Its `errors` are raised
# here; what to make of `data` is the caller's.
def response_body(response: httpx2.Response) -> ResponseBody:
    try:
        body = ResponseBody.model_validate(response.json())
    except pydantic.ValidationError as exc:
        raise GraphQLResponseError([
            {"message": f"Malformed response body: {exc}"}
        ]) from exc
    if body.errors:
        raise GraphQLResponseError(body.errors)
    return body
