import json
from pathlib import Path

import graphql
import pydantic

from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.testing import accept_graphql_ws

POSTS = [{"id": "10", "title": "Typed clients"}]


def _resolve_thumbnail(_info: graphql.GraphQLResolveInfo, **args: int) -> str:
    return f"https://cdn.example/pic-{args['width']}.png"


# Chapter 6's post. `__typename` is spelled out because the slot's payload is
# resolved from a plain mapping: graphql-core has no other way to tell which
# member of the `Attachment` union it is holding.
IMAGE_POST: dict[str, object] = {
    "id": "1",
    "title": "Slots, explained",
    "attachment": {
        "__typename": "ImageAttachment",
        "url": "https://cdn.example/pic.png",
        "caption": "A picture",
        "thumbnail": _resolve_thumbnail,
    },
}

ALICE: dict[str, object] = {
    "id": "1",
    "name": "Alice",
    "email": "alice@example.com",
    "phone": "+1 555 0100",
    "role": "ADMIN",
    "posts": POSTS,
}

SCHEMA = graphql.build_schema(
    (Path(__file__).parent / "schema.graphql").read_text(encoding="utf-8")
)


def _resolve_user(
    _info: graphql.GraphQLResolveInfo, **args: str
) -> dict[str, object] | None:
    return ALICE if args["id"] == ALICE["id"] else None


def _resolve_post(
    _info: graphql.GraphQLResolveInfo, **args: str
) -> dict[str, object] | None:
    return IMAGE_POST if args["id"] == IMAGE_POST["id"] else None


# graphql-core's default resolver calls a callable it finds under the field
# name, so the whole fake is a mapping -- no schema surgery.
ROOT = {"user": _resolve_user, "post": _resolve_post}

_BODY = pydantic.TypeAdapter(bytes)


class GraphQLRequest(pydantic.BaseModel):
    query: str
    variables: dict[str, object] | None = None


async def _read_body(receive: ASGIReceive) -> bytes:
    body = b""
    more_body = True
    while more_body:
        event = await receive()
        body += _BODY.validate_python(event["body"])
        more_body = bool(event.get("more_body"))
    return body


async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
    if scope["type"] == "http":
        request = GraphQLRequest.model_validate_json(await _read_body(receive))
        result = graphql.graphql_sync(  # pyright: ignore[reportUnknownMemberType] -- graphql-core types `middleware` via unparameterized Tuple/List
            SCHEMA, request.query, root_value=ROOT, variable_values=request.variables
        )
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": json.dumps(result.formatted).encode(),
        })
        return

    connection = await accept_graphql_ws(scope, receive, send)
    subscription = await connection.ack()
    await subscription.next({
        "postAdded": {
            "id": "11",
            "title": "Slots, explained",
            "body": "...",
            "author": {"name": ALICE["name"]},
        }
    })
    await subscription.complete()
    await connection.drain()
