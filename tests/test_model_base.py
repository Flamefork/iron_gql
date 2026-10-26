"""The consumer's base model: every model iron_gql parses the consumer's data
with is built on it -- the generated models and the runtime's own models of a
response and of a subscription message.

The base here hides the input in errors, the setting a consumer that logs its
errors needs, and sets two keys the library owns on purpose: the library's
value must win for each. Whether an error quotes its input is read off
pydantic's own rendering, which writes `input_value=` exactly when it does.
"""

import importlib
import json
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Literal
from typing import Protocol
from typing import cast

import pydantic
import pytest

from iron_gql import GraphQLResponseError
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.runtime import SubscriptionProtocol
from iron_gql.testing import accept_graphql_ws
from iron_gql.testing import use_client
from iron_gql.testing.server import async_live_asgi_server
from iron_gql.testing.server import live_asgi_server
from tests.conftest import ProjectBuilder
from tests.conftest import accept_graphql_sse
from tests.conftest import generated_package
from tests.conftest import make_subscription_app
from tests.corpus.graphql_over_http import BODIES
from tests.corpus.graphql_over_http import ERRORS_FORMS
from tests.corpus.graphql_over_http import MALFORMED_WS_MESSAGES
from tests.corpus.graphql_over_http import MEDIA_TYPES
from tests.corpus.graphql_over_http import STATUSES
from tests.corpus.graphql_over_http import ResponseCase
from tests.corpus.graphql_over_http import expected
from tests.corpus.model_bases import LIBRARY_KEY_BASES
from tests.corpus.model_bases import BaseForm

_SCHEMA = """
type Query {
    post: Post!
}

type Post {
    id: ID!
    postTitle: String!
    attachment: Attachment!
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}

type Subscription {
    counter: Int!
}
"""

# Every key but `hide_input_in_errors` is one the library sets itself, and
# set here to a value that shows if it got through: an upper-case alias for
# every field, no key the model does not declare, a dump by field name, no
# construction by field name -- which pydantic also stores as
# `validate_by_name=False`.
_SETTINGS = """
import pydantic

GRAPHQL_URL = "http://testserver/graphql/"


class Model(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        hide_input_in_errors=True,
        alias_generator=str.upper,
        extra="forbid",
        serialize_by_alias=False,
        populate_by_name=False,
    )
"""


def _queries(package: str) -> str:
    return f"""
    from tests.generated.{package}.gql.api import api_gql

    get_post = api_gql("query GetPost {{ post {{ id postTitle }} }}")
    get_attachment = api_gql(\"\"\"
        query GetAttachment {{
            post {{
                attachment {{
                    __typename
                    ... on ImageAttachment {{ url }}
                    ... on LinkAttachment {{ href }}
                }}
            }}
        }}
    \"\"\")
    get_slotted = api_gql(\"\"\"
        query GetSlotted {{ post {{ attachment @slot {{ __typename }} }} }}
    \"\"\")
    image_url = api_gql("fragment ImageUrl on ImageAttachment {{ url }}")
    get_slotted_image = get_slotted.bind(attachment=image_url)
    counter = api_gql("subscription Counter {{ counter }}")
    """


generated_package(
    "model_base",
    schema=_SCHEMA,
    queries=_queries("model_base"),
    settings=_SETTINGS,
    model_base_import="tests.generated.model_base.settings:Model",
)
generated_package(
    "model_base_sync",
    schema=_SCHEMA,
    queries=_queries("model_base_sync"),
    mode="sync",
    settings=_SETTINGS,
    model_base_import="tests.generated.model_base_sync.settings:Model",
)

from tests.generated.model_base import queries
from tests.generated.model_base import settings
from tests.generated.model_base.gql import api
from tests.generated.model_base_sync import queries as sync_queries
from tests.generated.model_base_sync.gql import api as sync_api

# A key the protocol lets a server add beside the ones the client reads:
# every message below carries one, which a base forbidding extra keys would
# refuse.
_EXTENSIONS = {"extensions": {"trace": "t-1"}}


def _response_app(body: object) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        request = await accept_graphql_sse(scope, receive, send)
        await request.respond(200, "application/json", json.dumps(body).encode())

    return app


def _query_app(data: dict[str, object]) -> ASGIApp:
    return _response_app({"data": data, **_EXTENSIONS})


def _sse_app(payload: object) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        request = await accept_graphql_sse(scope, receive, send)
        await request.open_stream()
        await request.write(f"event: next\ndata: {json.dumps(payload)}\n\n".encode())
        await request.complete()
        await request.end()

    return app


# Sends `message` as it is where the client waits for `connection_ack`, or
# for the subscription's first message.
def _ws_message_app(message: object, *, after_ack: bool) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        connection = await accept_graphql_ws(scope, receive, send)
        if after_ack:
            _ = await connection.ack()
        await send({"type": "websocket.send", "text": json.dumps(message)})
        await connection.drain()

    return app


@asynccontextmanager
async def _serving(
    app: ASGIApp, protocol: SubscriptionProtocol = "graphql-sse"
) -> AsyncIterator[None]:
    client = AsyncGQLClient(
        base_url="http://testserver/graphql",
        subscription_protocol=protocol,
        target_app=app,
    )
    async with use_client(api, client):
        yield


# graphql-sse needs a socket: the ASGI transport holds a body until the app
# returns.
@asynccontextmanager
async def _serving_live(app: ASGIApp) -> AsyncIterator[None]:
    async with async_live_asgi_server(app) as base_url:
        client = AsyncGQLClient(base_url=base_url, subscription_protocol="graphql-sse")
        async with use_client(api, client):
            yield


async def _subscribe() -> list[int]:
    async with queries.counter.execute() as stream:
        return [item.counter async for item in stream]


_IMAGE = {"__typename": "ImageAttachment", "url": "https://img"}


async def test_generated_models_read_through_library_keys():
    async with _serving(_query_app({"post": {"id": "1", "postTitle": "Hello"}})):
        post = (await queries.get_post.execute()).post
    async with _serving(_query_app({"post": {"attachment": _IMAGE}})):
        attachment = (await queries.get_attachment.execute()).post.attachment
    async with _serving(_query_app({"post": {"attachment": _IMAGE}})):
        slotted = (await queries.get_slotted_image.execute()).post.attachment
    assert (post.id, post.post_title) == ("1", "Hello")
    assert attachment.model_dump() == _IMAGE
    image = queries.image_url.read(slotted)
    assert image is not None
    assert image.url == "https://img"


# Each generated model a response passes through, with a value of the wrong
# type in it: a model, a member of a union, a slot node -- an open model --
# and the fragment read from it.
_MISTYPED: dict[str, tuple[Callable[[], Awaitable[object]], dict[str, object]]] = {
    "model": (
        queries.get_post.execute,
        {"post": {"id": "1", "postTitle": ["SECRET"]}},
    ),
    "union-member": (
        queries.get_attachment.execute,
        {"post": {"attachment": {"__typename": "ImageAttachment", "url": [1]}}},
    ),
    "slot-node": (
        queries.get_slotted_image.execute,
        {"post": {"attachment": {"__typename": ["ImageAttachment"]}}},
    ),
    "fragment": (
        queries.get_slotted_image.execute,
        {"post": {"attachment": {"__typename": "ImageAttachment", "url": [1]}}},
    ),
}


@pytest.mark.parametrize("case", list(_MISTYPED))
async def test_generated_model_error_hides_its_input(case: str):
    execute, data = _MISTYPED[case]
    async with _serving(_query_app(data)):
        with pytest.raises(pydantic.ValidationError) as caught:
            await execute()
    assert "input_value" not in str(caught.value)


type _Mode = Literal["async", "sync"]
_MODES: tuple[_Mode, ...] = ("async", "sync")
_PROTOCOLS: tuple[SubscriptionProtocol, ...] = ("graphql-sse", "graphql-transport-ws")


# The counters a subscription of the module in `mode` reads from `app`, or the
# error it ends with. graphql-sse needs a socket: the ASGI transport holds a
# body until the app returns; the sync client takes no app at all.
async def _subscription(
    mode: _Mode, protocol: SubscriptionProtocol, app: ASGIApp
) -> list[int] | GraphQLResponseError:
    try:
        match mode:
            case "async" if protocol == "graphql-sse":
                async with _serving_live(app):
                    return await _subscribe()
            case "async":
                async with _serving(app, protocol):
                    return await _subscribe()
            case "sync":
                with (
                    live_asgi_server(app) as base_url,
                    use_client(
                        sync_api,
                        GQLClient(base_url=base_url, subscription_protocol=protocol),
                    ),
                    sync_queries.counter.execute() as stream,
                ):
                    return [item.counter for item in stream]
    except GraphQLResponseError as exc:
        return exc


def _next_app(protocol: SubscriptionProtocol, payload: object) -> ASGIApp:
    match protocol:
        case "graphql-sse":
            return _sse_app(payload)
        case "graphql-transport-ws":
            return make_subscription_app([
                {"type": "next", "payload": payload},
                {"type": "complete"},
            ])


def _assert_hides_input(result: object, prefix: str) -> None:
    assert isinstance(result, GraphQLResponseError), result
    assert str(result).startswith(prefix)
    assert "input_value" not in str(result)


@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("mode", _MODES)
async def test_protocol_models_read_through_library_keys(
    mode: _Mode, protocol: SubscriptionProtocol
):
    payload: dict[str, object] = {"data": {"counter": 1}, **_EXTENSIONS}
    assert await _subscription(mode, protocol, _next_app(protocol, payload)) == [1]


# The malformed `errors` entries of the corpus.
_MALFORMED_ERRORS: dict[str, object] = {
    form.name: form.value for form in ERRORS_FORMS if form.kind == "malformed"
}

# The JSON bodies of the corpus that are no GraphQL response, by its own
# table, each also sent as a subscription's `next` payload.
_MALFORMED_BODIES: dict[str, object] = {
    body.name: json.loads(body.content)
    for body in BODIES
    if body.name != "not-json"
    and expected(
        ResponseCase(status=STATUSES[0], media_type=MEDIA_TYPES[0], body=body),
        subscription=False,
    )
    == "malformed"
}


@pytest.mark.parametrize("body", list(_MALFORMED_BODIES))
async def test_async_response_error_hides_its_input(body: str):
    async with _serving(_response_app(_MALFORMED_BODIES[body])):
        with pytest.raises(GraphQLResponseError) as caught:
            await queries.get_post.execute()
    _assert_hides_input(caught.value, "Malformed response body")


@pytest.mark.parametrize("body", list(_MALFORMED_BODIES))
def test_sync_response_error_hides_its_input(body: str):
    with (
        live_asgi_server(_response_app(_MALFORMED_BODIES[body])) as base_url,
        use_client(
            sync_api,
            GQLClient(base_url=base_url, subscription_protocol="graphql-sse"),
        ),
        pytest.raises(GraphQLResponseError) as caught,
    ):
        sync_queries.get_post.execute()
    _assert_hides_input(caught.value, "Malformed response body")


# A single response in place of the stream is read the way a query's is.
@pytest.mark.parametrize("body", list(_MALFORMED_BODIES))
@pytest.mark.parametrize("mode", _MODES)
async def test_refused_stream_error_hides_its_input(mode: _Mode, body: str):
    app = _response_app(_MALFORMED_BODIES[body])
    result = await _subscription(mode, "graphql-sse", app)
    _assert_hides_input(result, "Malformed response body")


@pytest.mark.parametrize("body", list(_MALFORMED_BODIES))
@pytest.mark.parametrize("protocol", _PROTOCOLS)
@pytest.mark.parametrize("mode", _MODES)
async def test_next_error_hides_its_input(
    mode: _Mode, protocol: SubscriptionProtocol, body: str
):
    app = _next_app(protocol, _MALFORMED_BODIES[body])
    result = await _subscription(mode, protocol, app)
    _assert_hides_input(result, "Malformed next payload")


@pytest.mark.parametrize("errors", list(_MALFORMED_ERRORS))
@pytest.mark.parametrize("mode", _MODES)
async def test_ws_error_payload_hides_its_input(mode: _Mode, errors: str):
    app = make_subscription_app([
        {"type": "error", "payload": _MALFORMED_ERRORS[errors]}
    ])
    result = await _subscription(mode, "graphql-transport-ws", app)
    _assert_hides_input(result, "Malformed error payload")


@pytest.mark.parametrize("message", list(MALFORMED_WS_MESSAGES))
@pytest.mark.parametrize("mode", _MODES)
async def test_ws_handshake_message_error_hides_its_input(mode: _Mode, message: str):
    app = _ws_message_app(MALFORMED_WS_MESSAGES[message], after_ack=False)
    result = await _subscription(mode, "graphql-transport-ws", app)
    _assert_hides_input(result, "Malformed protocol message during handshake")


@pytest.mark.parametrize("message", list(MALFORMED_WS_MESSAGES))
@pytest.mark.parametrize("mode", _MODES)
async def test_ws_stream_message_error_hides_its_input(mode: _Mode, message: str):
    app = _ws_message_app(MALFORMED_WS_MESSAGES[message], after_ack=True)
    result = await _subscription(mode, "graphql-transport-ws", app)
    _assert_hides_input(result, "Malformed protocol message:")


async def test_use_client_gives_its_base_to_a_client_without_one():
    client = AsyncGQLClient(
        base_url="http://testserver", subscription_protocol="graphql-sse"
    )
    async with use_client(api, client):
        assert client.model_base is settings.Model


async def test_use_client_takes_a_client_on_the_module_base():
    client = AsyncGQLClient(
        base_url="http://testserver",
        subscription_protocol="graphql-sse",
        model_base=settings.Model,
    )
    async with use_client(api, client):
        assert client.model_base is settings.Model


@pytest.mark.parametrize("base", [pydantic.BaseModel, settings.Model])
def test_use_client_refuses_a_client_on_another_base(base: type[pydantic.BaseModel]):
    client = GQLClient(
        base_url="http://testserver",
        subscription_protocol="graphql-sse",
        model_base=base,
    )
    previous = cast("GQLClient", vars(sync_api)["_client"])
    with pytest.raises(ValueError, match="pass the module's base or none"):  # noqa: SIM117
        with use_client(sync_api, client):
            pass
    assert cast("GQLClient", vars(sync_api)["_client"]) is previous


# The generated module imports the base's module whole and spells the base by
# its dotted path, so the root of that path is a name the module binds.
def test_schema_name_of_the_base_root_module_is_rejected(test_project: ProjectBuilder):
    test_project.prepare(
        schema="""
        type Query {
            entry: Entry
        }

        type Entry {
            kind: corebase!
        }

        enum corebase {
            FIRST
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        get_entry = api_gql("query GetEntry { entry { kind } }")
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="'corebase' is claimed by"):
        test_project.generate(model_base_import="corebase.models:Model")


_BASE_FORM_SCHEMA = """
type Query {
    findPost(filter: PostFilter!): Post
}

type Post {
    postTitle: String!
}

input PostFilter {
    postTitle: String!
}
"""

_BASE_FORM_QUERIES = """
from sample_app.gql.api import api_gql

find_post = api_gql(
    \"\"\"
    query FindPost($filter: PostFilter!) {
        findPost(filter: $filter) { postTitle }
    }
    \"\"\"
)
"""


# A server that answers `post` for every request and keeps the variables each
# one sent.
def _recording_app(post: dict[str, object], sent: list[object]) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        request = await accept_graphql_sse(scope, receive, send)
        sent.append(request.body.get("variables"))
        body = {"data": {"findPost": post}, **_EXTENSIONS}
        await request.respond(200, "application/json", json.dumps(body).encode())

    return app


class _FindPost(Protocol):
    async def execute(self, *, filter: pydantic.BaseModel) -> pydantic.BaseModel: ...


def _base_module(form: BaseForm) -> str:
    return "\n".join([
        "import pydantic",
        "",
        "",
        "class Model(pydantic.BaseModel):",
        f"    model_config = pydantic.ConfigDict({form.config})",
        "",
    ])


@pytest.mark.parametrize(
    "form", LIBRARY_KEY_BASES, ids=[form.name for form in LIBRARY_KEY_BASES]
)
async def test_library_keys_win_over_the_base(
    test_project: ProjectBuilder, form: BaseForm
):
    test_project.prepare(schema=_BASE_FORM_SCHEMA, queries=_BASE_FORM_QUERIES)
    test_project.write_file(
        test_project.root / "sample_app/models.py", _base_module(form)
    )
    test_project.generate(model_base_import="sample_app.models:Model")
    generated = test_project.import_api()
    queries_module = importlib.import_module("sample_app.queries")
    find_post = cast("_FindPost", vars(queries_module)["find_post"])
    post_filter = cast("type[pydantic.BaseModel]", vars(generated)["PostFilter"])
    sent: list[object] = []

    async def execute(post: dict[str, object]) -> pydantic.BaseModel:
        client = AsyncGQLClient(
            base_url="http://testserver/graphql",
            subscription_protocol="graphql-sse",
            target_app=_recording_app(post, sent),
        )
        async with use_client(generated, client):
            return await find_post.execute(filter=post_filter(post_title="Hello"))

    result = await execute({"postTitle": "Hi"})
    assert result.model_dump() == {"findPost": {"postTitle": "Hi"}}
    assert sent == [{"filter": {"postTitle": "Hello"}}]
    with pytest.raises(pydantic.ValidationError) as caught:
        await execute({"postTitle": "Hi", "unknown": 1})
    assert caught.value.errors()[0]["type"] == "extra_forbidden"
