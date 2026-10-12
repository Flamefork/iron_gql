from typing import Any

import httpx2
import pydantic

from iron_gql.errors import GraphQLResponseError


class ResponseBody(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore")
    data: dict[str, Any] | None = None
    errors: list[dict[str, Any]] | None = None


def raise_for_status(response: httpx2.Response) -> None:
    if httpx2.codes.is_redirect(response.status_code):
        location: str = response.headers.get("Location", "")
        message = f"Unexpected 3xx response ({response.status_code})"
        if location:
            message = f"{message} to {location}"
        raise httpx2.HTTPStatusError(
            message, request=response.request, response=response
        )
    response.raise_for_status()


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
