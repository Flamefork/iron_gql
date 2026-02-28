import asyncio
import datetime
import io
import json
from collections.abc import MutableMapping
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pydantic
import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Response

from iron_gql import FileVar
from iron_gql import GraphQLResponseError
from iron_gql import websockets
from iron_gql.runtime import GQLClient
from iron_gql.runtime import GQLOperation
from iron_gql.runtime import extract_files
from iron_gql.runtime import serialize_var
from tests.conftest import ProjectBuilder


async def test_generate_and_execute_queries(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user(id: ID!): User
        }

        type Mutation {
            updateUser(input: UpdateUserInput!): User
        }

        type User {
            id: ID!
            name: String!
        }

        input UpdateUserInput {
            id: ID!
            name: String!
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    id
                    name
                }
            }
            '''
        )

        update_user = api_gql(
            '''
            mutation UpdateUser($input: UpdateUserInput!) {
                updateUser(input: $input) {
                    id
                    name
                }
            }
            '''
        )
    """

    state = {"user-1": "Graph"}

    def resolve_user(_root, _info, *, id: str):
        name = state.get(id)
        if name is None:
            return None
        return {"id": id, "name": name}

    def resolve_update_user(_root, _info, **kwargs):
        input_data = kwargs["input"]
        user_id = str(input_data["id"])
        state[user_id] = input_data["name"]
        return {"id": user_id, "name": input_data["name"]}

    async with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={
            "Query": {"user": resolve_user},
            "Mutation": {"updateUser": resolve_update_user},
        },
    ) as (api, queries):
        read_query = queries.get_user.with_headers({"Authorization": "Bearer token"})
        initial = await read_query.execute(id="user-1")
        assert initial.user is not None
        assert initial.user.name == "Graph"

        mutation_input = api.UpdateUserInput(id="user-1", name="Morty")
        updated = await queries.update_user.execute(input=mutation_input)
        assert updated.update_user.name == "Morty"
        refreshed = await queries.get_user.execute(id="user-1")
        assert refreshed.user is not None
        assert refreshed.user.name == "Morty"


async def test_list_allows_null_elements(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            numbers1: [Int]!
            numbers2: [Int!]
        }
    """

    call_count = 0

    def resolve_numbers1(_root, _info):
        nonlocal call_count
        if call_count == 0:
            return [1, None]  # valid: [Int]! allows null elements
        return [1, 2]

    def resolve_numbers2(_root, _info):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [1, 2]  # valid: [Int!] doesn't allow null elements
        return [1, None]  # invalid: null in [Int!]

    async with test_project.server(
        httpserver,
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        numbers = api_gql(
            '''
            query Numbers {
                numbers1
                numbers2
            }
            '''
        )
        """,
        resolvers={
            "Query": {"numbers1": resolve_numbers1, "numbers2": resolve_numbers2}
        },
    ) as (_, queries):
        response = await queries.numbers.execute()
        assert response.numbers_1 == [1, None]
        assert response.numbers_2 == [1, 2]

        with pytest.raises(GraphQLResponseError):
            await queries.numbers.execute()


async def test_variable_defaults_optional(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            posts(limit: Int = 5): [Int!]!
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        get_posts = api_gql(
            '''
            query GetPosts($limit: Int = 5) {
                posts(limit: $limit)
            }
            '''
        )
    """

    def resolve_posts(_root, _info, *, limit: int = 5):
        return list(range(limit))

    async with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"posts": resolve_posts}},
    ) as (_, queries):
        default_result = await queries.get_posts.execute()
        assert default_result.posts == [0, 1, 2, 3, 4]

        explicit_result = await queries.get_posts.execute(limit=2)
        assert explicit_result.posts == [0, 1]


def test_serialize_var_handles_nested_structures():
    nested_list = [[1, 2], [3, 4]]
    assert serialize_var(nested_list) == [[1, 2], [3, 4]]

    nested_dict = {"a": {"b": 1}, "c": [1, 2]}
    assert serialize_var(nested_dict) == {"a": {"b": 1}, "c": [1, 2]}

    mixed = {"a": [1, {"b": 2}]}
    assert serialize_var(mixed) == {"a": [1, {"b": 2}]}


def test_serialize_var_handles_custom_scalar_types():
    dt = datetime.datetime(2024, 1, 15, 10, 30, 0, tzinfo=datetime.UTC)
    assert serialize_var(dt) == "2024-01-15T10:30:00Z"

    d = datetime.date(2024, 6, 1)
    assert serialize_var(d) == "2024-06-01"

    assert serialize_var(Decimal("12.50")) == "12.50"

    assert serialize_var({"dates": [dt, d]}) == {
        "dates": ["2024-01-15T10:30:00Z", "2024-06-01"]
    }


def test_query_auto_detects_file_uploads():
    variables = {"name": "test", "file": FileVar(b"data", filename="f.txt")}
    nulled, files = extract_files(variables)
    assert nulled["name"] == "test"
    assert nulled["file"] is None
    assert "variables.file" in files


async def test_close(test_project: ProjectBuilder, httpserver: HTTPServer):
    schema = """
        type Query {
            ping: String!
        }
    """

    def resolve_ping(_root, _info):
        return "pong"

    async with test_project.server(
        httpserver,
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        ping = api_gql(
            '''
            query Ping {
                ping
            }
            '''
        )
        """,
        resolvers={"Query": {"ping": resolve_ping}},
    ) as (api, queries):
        await queries.ping.execute()
        assert not api.API_CLIENT._client.is_closed  # noqa: SLF001

        await api.API_CLIENT.close()
        assert api.API_CLIENT._client.is_closed  # noqa: SLF001


def test_extract_files_single():
    f = io.BytesIO(b"hello")
    variables = {"file": FileVar(f, filename="test.txt")}
    nulled, files = extract_files(variables)
    assert nulled == {"file": None}
    assert list(files.keys()) == ["variables.file"]
    assert files["variables.file"].f is f
    assert files["variables.file"].filename == "test.txt"


def test_extract_files_nested():
    f1 = io.BytesIO(b"one")
    f2 = io.BytesIO(b"two")
    variables = {
        "input": {"avatar": FileVar(f1, filename="a.png"), "name": "Morty"},
        "cover": FileVar(f2, filename="c.jpg", content_type="image/jpeg"),
    }
    nulled, files = extract_files(variables)
    assert nulled == {"input": {"avatar": None, "name": "Morty"}, "cover": None}
    assert "variables.input.avatar" in files
    assert "variables.cover" in files
    assert files["variables.input.avatar"].f is f1
    assert files["variables.cover"].content_type == "image/jpeg"


def test_extract_files_in_list():
    f1 = io.BytesIO(b"a")
    f2 = io.BytesIO(b"b")
    variables = {"files": [FileVar(f1), FileVar(f2)]}
    nulled, files = extract_files(variables)
    assert nulled == {"files": [None, None]}
    assert "variables.files.0" in files
    assert "variables.files.1" in files


def test_extract_files_no_files():
    variables = {"name": "Morty", "age": 14}
    nulled, files = extract_files(variables)
    assert nulled == {"name": "Morty", "age": 14}
    assert files == {}


async def test_graphql_response_error(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            fail: String!
        }
    """

    def graphql_error_handler(_request):
        return Response(
            json.dumps({"data": None, "errors": [{"message": "boom"}]}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        graphql_error_handler
    )
    base_url = httpserver.url_for("/graphql/")
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        fail = api_gql(
            '''
            query Fail {
                fail
            }
            '''
        )
        """,
        base_url=base_url,
    )
    _, queries_module = test_project.generate_and_import()
    try:
        with pytest.raises(GraphQLResponseError, match="boom"):
            await queries_module.fail.execute()
    finally:
        await test_project.import_api().API_CLIENT.close()


async def test_partial_errors_raise(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            ok: String!
        }
    """

    def partial_error_handler(_request):
        return Response(
            json.dumps({
                "data": {"ok": "value"},
                "errors": [{"message": "partial failure"}],
            }),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        partial_error_handler
    )
    base_url = httpserver.url_for("/graphql/")
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        ok = api_gql(
            '''
            query Ok {
                ok
            }
            '''
        )
        """,
        base_url=base_url,
    )
    _, queries_module = test_project.generate_and_import()
    try:
        with pytest.raises(GraphQLResponseError, match="partial failure"):
            await queries_module.ok.execute()
    finally:
        await test_project.import_api().API_CLIENT.close()


async def test_file_upload_multipart(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        scalar Upload

        type Query {
            _dummy: String
        }

        type Mutation {
            uploadFile(file: Upload!, label: String!): String!
        }
    """

    received: dict = {}

    def upload_handler(request):
        received["operations"] = json.loads(request.form["operations"])
        received["map"] = json.loads(request.form["map"])
        received["file_content"] = request.files["0"].read().decode()
        received["file_name"] = request.files["0"].filename
        return Response(
            json.dumps({"data": {"uploadFile": f"ok:{received['file_content']}"}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        upload_handler
    )
    base_url = httpserver.url_for("/graphql/")
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        upload_file = api_gql(
            '''
            mutation UploadFile($file: Upload!, $label: String!) {
                uploadFile(file: $file, label: $label)
            }
            '''
        )
        """,
        base_url=base_url,
    )
    api_module, queries_module = test_project.generate_and_import()
    try:
        file_data = io.BytesIO(b"hello world")
        result = await queries_module.upload_file.execute(
            file=FileVar(file_data, filename="test.txt"), label="my-label"
        )

        assert result.upload_file == "ok:hello world"
        assert received["operations"]["variables"]["file"] is None
        assert received["operations"]["variables"]["label"] == "my-label"
        assert received["map"] == {"0": ["variables.file"]}
        assert received["file_content"] == "hello world"
        assert received["file_name"] == "test.txt"
    finally:
        await api_module.API_CLIENT.close()


async def test_custom_scalar_in_variables_and_response(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        scalar DateTime

        type Query {
            events(since: DateTime!): [Event!]!
        }

        type Event {
            name: String!
            startedAt: DateTime!
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        get_events = api_gql(
            '''
            query GetEvents($since: DateTime!) {
                events(since: $since) {
                    name
                    startedAt
                }
            }
            '''
        )
    """

    def resolve_events(_root, _info, *, since: str):
        return [{"name": "Launch", "startedAt": since}]

    async with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"events": resolve_events}},
    ) as (_, queries):
        dt = datetime.datetime(2024, 1, 15, 10, 30, 0, tzinfo=datetime.UTC)
        result = await queries.get_events.execute(since=dt)
        assert result.events[0].name == "Launch"
        assert result.events[0].started_at == dt


async def test_base_url_without_trailing_slash_calls_exact_endpoint(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            ping: String!
        }
    """

    def ping_handler(_request):
        return Response(
            json.dumps({"data": {"ping": "pong"}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql", method="POST").respond_with_handler(
        ping_handler
    )
    base_url = httpserver.url_for("/graphql")
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        ping = api_gql(
            '''
            query Ping {
                ping
            }
            '''
        )
        """,
        base_url=base_url,
    )
    api_module, queries_module = test_project.generate_and_import()
    try:
        result = await queries_module.ping.execute()
        assert result.ping == "pong"
    finally:
        await api_module.API_CLIENT.close()


async def test_file_upload_multipart_base_url_without_trailing_slash(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        scalar Upload

        type Query {
            _dummy: String
        }

        type Mutation {
            uploadFile(file: Upload!, label: String!): String!
        }
    """

    received: dict = {}

    def upload_handler(request):
        received["operations"] = json.loads(request.form["operations"])
        received["map"] = json.loads(request.form["map"])
        received["file_content"] = request.files["0"].read().decode()
        received["file_name"] = request.files["0"].filename
        return Response(
            json.dumps({"data": {"uploadFile": f"ok:{received['file_content']}"}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql", method="POST").respond_with_handler(
        upload_handler
    )
    base_url = httpserver.url_for("/graphql")
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        upload_file = api_gql(
            '''
            mutation UploadFile($file: Upload!, $label: String!) {
                uploadFile(file: $file, label: $label)
            }
            '''
        )
        """,
        base_url=base_url,
    )
    api_module, queries_module = test_project.generate_and_import()
    try:
        file_data = io.BytesIO(b"hello world")
        result = await queries_module.upload_file.execute(
            file=FileVar(file_data, filename="test.txt"), label="my-label"
        )

        assert result.upload_file == "ok:hello world"
        assert received["operations"]["variables"]["file"] is None
        assert received["operations"]["variables"]["label"] == "my-label"
        assert received["map"] == {"0": ["variables.file"]}
        assert received["file_content"] == "hello world"
        assert received["file_name"] == "test.txt"
    finally:
        await api_module.API_CLIENT.close()


async def test_redirect_response_raises_http_status_error(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            ping: String!
        }
    """

    def redirect_handler(_request):
        return Response(
            "moved",
            status=307,
            headers={"Location": "/graphql/"},
            mimetype="text/plain",
        )

    httpserver.expect_request("/graphql", method="POST").respond_with_handler(
        redirect_handler
    )
    base_url = httpserver.url_for("/graphql")
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        ping = api_gql(
            '''
            query Ping {
                ping
            }
            '''
        )
        """,
        base_url=base_url,
    )
    api_module, queries_module = test_project.generate_and_import()
    try:
        with pytest.raises(
            httpx.HTTPStatusError,
            match=r"Unexpected 3xx response \(307\) to /graphql/",
        ):
            await queries_module.ping.execute()
    finally:
        await api_module.API_CLIENT.close()


async def test_redirect_without_location_header(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            ping: String!
        }
    """

    def redirect_handler(_request):
        return Response("moved", status=302, mimetype="text/plain")

    httpserver.expect_request("/graphql", method="POST").respond_with_handler(
        redirect_handler
    )
    base_url = httpserver.url_for("/graphql")
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        ping = api_gql("query Ping { ping }")
        """,
        base_url=base_url,
    )
    api_module, queries_module = test_project.generate_and_import()
    try:
        with pytest.raises(
            httpx.HTTPStatusError,
            match=r"Unexpected 3xx response \(302\)",
        ):
            await queries_module.ping.execute()
    finally:
        await api_module.API_CLIENT.close()


async def test_no_data_in_response(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            ping: String!
        }
    """

    def no_data_handler(_request):
        return Response(
            json.dumps({"extensions": {"tracing": True}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        no_data_handler
    )
    base_url = httpserver.url_for("/graphql/")
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        ping = api_gql("query Ping { ping }")
        """,
        base_url=base_url,
    )
    _, queries_module = test_project.generate_and_import()
    try:
        with pytest.raises(GraphQLResponseError, match="No data in response"):
            await queries_module.ping.execute()
    finally:
        await test_project.import_api().API_CLIENT.close()


async def test_one_of_input_runtime(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            search(criteria: SearchCriteria!): String!
        }

        input SearchCriteria @oneOf {
            name: String
            email: String
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            query Search($criteria: SearchCriteria!) {
                search(criteria: $criteria)
            }
            '''
        )
    """

    def resolve_search(_root, _info, *, criteria: dict):
        if "name" in criteria:
            return f"found by name: {criteria['name']}"
        if "email" in criteria:
            return f"found by email: {criteria['email']}"
        return "not found"

    async with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"search": resolve_search}},
    ) as (api, queries):
        by_name = api.SearchCriteriaName(name="Alice")
        result = await queries.search.execute(criteria=by_name)
        assert result.search == "found by name: Alice"

        by_email = api.SearchCriteriaEmail(email="bob@example.com")
        result = await queries.search.execute(criteria=by_email)
        assert result.search == "found by email: bob@example.com"


async def test_file_upload_with_content_type(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        scalar Upload

        type Query {
            _dummy: String
        }

        type Mutation {
            uploadFile(file: Upload!): String!
        }
    """

    received: dict = {}

    def upload_handler(request):
        received["content_type"] = request.files["0"].content_type
        received["file_name"] = request.files["0"].filename
        return Response(
            json.dumps({"data": {"uploadFile": "ok"}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        upload_handler
    )
    base_url = httpserver.url_for("/graphql/")
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        upload = api_gql(
            '''
            mutation Upload($file: Upload!) {
                uploadFile(file: $file)
            }
            '''
        )
        """,
        base_url=base_url,
    )
    api_module, queries_module = test_project.generate_and_import()
    try:
        file_data = io.BytesIO(b"image data")
        result = await queries_module.upload.execute(
            file=FileVar(file_data, filename="photo.png", content_type="image/png")
        )
        assert result.upload_file == "ok"
        assert received["content_type"] == "image/png"
        assert received["file_name"] == "photo.png"
    finally:
        await api_module.API_CLIENT.close()


def _make_ws_app(  # noqa: C901
    messages: list[dict[str, Any]],
    *,
    pre_ack_messages: list[dict[str, Any]] | None = None,
    ack_response: dict[str, str] | None = None,
    captured: dict[str, Any] | None = None,
) -> Any:
    async def app(  # noqa: C901
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        if captured is not None:
            scope_headers = [
                (k.decode(), v.decode()) for k, v in scope.get("headers", [])
            ]
            captured["scope_raw_headers"] = scope_headers
            captured["scope_headers"] = dict(scope_headers)
            captured["scope_query_string"] = scope.get("query_string", b"").decode()
        subprotocols = scope.get("subprotocols", [])
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })

        init_msg = json.loads((await receive())["text"])
        assert init_msg["type"] == "connection_init"
        for msg in pre_ack_messages or []:
            await send({
                "type": "websocket.send",
                "text": json.dumps(msg),
            })
            if msg.get("type") == "ping":
                pong_msg = json.loads((await receive())["text"])
                if captured is not None:
                    captured.setdefault("client_responses", []).append(pong_msg)
        ack = ack_response or {"type": "connection_ack"}
        await send({
            "type": "websocket.send",
            "text": json.dumps(ack),
        })

        if ack.get("type") != "connection_ack":
            await receive()
            return

        subscribe_msg = json.loads((await receive())["text"])
        assert subscribe_msg["type"] == "subscribe"
        sub_id = subscribe_msg["id"]
        if captured is not None:
            captured["subscribe"] = subscribe_msg

        for msg in messages:
            if msg.get("type") == "ping":
                await send({
                    "type": "websocket.send",
                    "text": json.dumps(msg),
                })
                pong_msg = json.loads((await receive())["text"])
                if captured is not None:
                    captured.setdefault("client_responses", []).append(pong_msg)
            elif msg.get("type") == "pong":
                await send({
                    "type": "websocket.send",
                    "text": json.dumps(msg),
                })
            else:
                await send({
                    "type": "websocket.send",
                    "text": json.dumps({"id": sub_id, **msg}),
                })

        await receive()

    return app


class _CounterResult(pydantic.BaseModel):
    counter: int


class _PingResult(pydantic.BaseModel):
    ping: str


async def test_subscribe_asgi():
    app = _make_ws_app([
        {"type": "next", "payload": {"data": {"counter": 1}}},
        {"type": "next", "payload": {"data": {"counter": 2}}},
        {"type": "next", "payload": {"data": {"counter": 3}}},
        {"type": "complete"},
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }"
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1, 2, 3]
    finally:
        await client.close()


async def test_subscribe_error():
    app = _make_ws_app([
        {"type": "next", "payload": {"data": {"counter": 1}}},
        {"type": "error", "payload": [{"message": "boom"}]},
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        results: list[int] = []

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for item in stream:
                    results.append(item.counter)  # noqa: PERF401

        with pytest.raises(GraphQLResponseError, match="boom"):
            await consume()

        assert results == [1]
    finally:
        await client.close()


async def test_subscribe_next_with_errors():
    app = _make_ws_app([
        {
            "type": "next",
            "payload": {"data": None, "errors": [{"message": "partial"}]},
        },
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="partial"):
            await consume()
    finally:
        await client.close()


def test_gql_operation_with_headers():
    op = GQLOperation()
    assert op.headers == {}

    op2 = op.with_headers({"Authorization": "Bearer token"})
    assert op2.headers == {"Authorization": "Bearer token"}
    assert op.headers == {}


async def test_subscribe_codegen_asgi(test_project: ProjectBuilder):
    messages = [
        {
            "type": "next",
            "payload": {"data": {"events": {"id": "1", "message": "hello"}}},
        },
        {
            "type": "next",
            "payload": {"data": {"events": {"id": "2", "message": "world"}}},
        },
        {"type": "complete"},
    ]

    test_project.prepare(
        schema="""
            type Query {
                _dummy: String
            }

            type Subscription {
                events(channel: String!): Event!
            }

            type Event {
                id: ID!
                message: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        events = api_gql(
            '''
            subscription Events($channel: String!) {
                events(channel: $channel) {
                    id
                    message
                }
            }
            '''
        )
        """,
    )

    captured: dict[str, Any] = {}
    app = _make_ws_app(messages, captured=captured)

    api_module, queries_module = test_project.generate_and_import()
    api_module.API_CLIENT = GQLClient(  # pyright: ignore[reportAttributeAccessIssue]
        base_url="http://testserver/graphql", target_app=app
    )
    try:
        results = [
            (item.events.id, item.events.message)
            async for item in queries_module.events.execute(channel="test")
        ]
        assert results == [("1", "hello"), ("2", "world")]
        assert captured["subscribe"]["payload"]["variables"] == {"channel": "test"}
    finally:
        await api_module.API_CLIENT.close()


async def test_subscribe_connection_rejected():
    app = _make_ws_app(
        [],
        ack_response={"type": "connection_error", "payload": "Unauthorized"},
    )

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="Expected connection_ack"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_no_data_in_next():
    app = _make_ws_app([
        {"type": "next", "payload": {"data": None}},
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="No data in response"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_unexpected_message_type():
    app = _make_ws_app([
        {"type": "unknown_type"},
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="Unexpected subscription message type"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_with_headers_propagation():
    captured: dict[str, Any] = {}
    app = _make_ws_app(
        [{"type": "complete"}],
        captured=captured,
    )

    client = GQLClient(
        base_url="http://testserver/graphql",
        target_app=app,
        headers={"X-Base": "base-value"},
    )
    try:
        async with client.subscribe(
            _CounterResult,
            "subscription { counter }",
            headers={"Authorization": "Bearer token"},
        ) as stream:
            async for _ in stream:
                pass
        assert captured["scope_headers"]["authorization"] == "Bearer token"
        assert captured["scope_headers"]["x-base"] == "base-value"
    finally:
        await client.close()


async def test_subscribe_ping_pong():
    captured: dict[str, Any] = {}
    app = _make_ws_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
            {"type": "ping"},
            {"type": "next", "payload": {"data": {"counter": 2}}},
            {"type": "next", "payload": {"data": {"counter": 3}}},
            {"type": "complete"},
        ],
        captured=captured,
    )

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }"
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1, 2, 3]
        assert captured["client_responses"] == [{"type": "pong"}]
    finally:
        await client.close()


async def test_subscribe_ping_before_connection_ack():
    captured: dict[str, Any] = {}
    app = _make_ws_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
            {"type": "complete"},
        ],
        pre_ack_messages=[{"type": "ping"}],
        captured=captured,
    )

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }"
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1]
        assert captured["client_responses"] == [{"type": "pong"}]
    finally:
        await client.close()


async def test_subscribe_with_headers_override_case_insensitive():
    captured: dict[str, Any] = {}
    app = _make_ws_app([{"type": "complete"}], captured=captured)

    client = GQLClient(
        base_url="http://testserver/graphql",
        target_app=app,
        headers={"Authorization": "Bearer base-token"},
    )
    try:
        async with client.subscribe(
            _CounterResult,
            "subscription { counter }",
            headers={"authorization": "Bearer override-token"},
        ) as stream:
            async for _ in stream:
                pass
        authorization_headers = [
            value
            for key, value in captured["scope_raw_headers"]
            if key == "authorization"
        ]
        assert authorization_headers == ["Bearer override-token"]
    finally:
        await client.close()


async def test_subscribe_carries_http_cookies():
    captured: dict[str, Any] = {}

    async def app(
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http":
            request = await receive()
            assert request["type"] == "http.request"
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"set-cookie", b"session=abc; Path=/; HttpOnly"],
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"data":{"ping":"pong"}}',
            })
            return

        assert scope["type"] == "websocket"
        scope_headers = [(k.decode(), v.decode()) for k, v in scope.get("headers", [])]
        captured["scope_headers"] = dict(scope_headers)
        subprotocols = scope.get("subprotocols", [])
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = json.loads((await receive())["text"])
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.send",
            "text": json.dumps({"type": "connection_ack"}),
        })
        subscribe_msg = json.loads((await receive())["text"])
        assert subscribe_msg["type"] == "subscribe"
        await send({
            "type": "websocket.send",
            "text": json.dumps({"id": subscribe_msg["id"], "type": "complete"}),
        })
        await receive()

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        result = await client.query(_PingResult, "query { ping }")
        assert result.ping == "pong"

        async with client.subscribe(
            _CounterResult, "subscription { counter }"
        ) as stream:
            async for _ in stream:
                pass

        assert "session=abc" in captured["scope_headers"]["cookie"]
    finally:
        await client.close()


async def test_subscribe_disconnect_before_connection_ack():
    async def app(
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope.get("subprotocols", [])
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = json.loads((await receive())["text"])
        assert init_msg["type"] == "connection_init"
        await send({"type": "websocket.close", "code": 4401, "reason": "Unauthorized"})

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="before connection_ack with code 4401"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_connection_ack_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(websockets, "_WS_CONNECTION_ACK_TIMEOUT_SECONDS", 0.01)

    async def app(
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope.get("subprotocols", [])
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = json.loads((await receive())["text"])
        assert init_msg["type"] == "connection_init"
        await asyncio.sleep(0.1)

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="Timed out waiting for connection_ack"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_url_scheme_https_to_wss():
    captured: dict[str, Any] = {}
    app = _make_ws_app([{"type": "complete"}], captured=captured)

    client = GQLClient(
        base_url="https://testserver/graphql?redirect=http://callback",
        target_app=app,
    )
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }"
        ) as stream:
            async for _ in stream:
                pass
        assert parse_qs(captured["scope_query_string"])["redirect"] == [
            "http://callback"
        ]
    finally:
        await client.close()


async def test_subscribe_malformed_message_no_type():
    app = _make_ws_app([
        {"payload": "no type field here"},
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="Unexpected subscription message type: None"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_no_variables_key_when_none():
    captured: dict[str, Any] = {}
    app = _make_ws_app([{"type": "complete"}], captured=captured)

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }"
        ) as stream:
            async for _ in stream:
                pass
        assert "variables" not in captured["subscribe"]["payload"]
    finally:
        await client.close()


async def test_subscribe_error_without_payload():
    app = _make_ws_app([
        {"type": "error"},
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="Error without payload"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_pong_during_messages():
    app = _make_ws_app([
        {"type": "next", "payload": {"data": {"counter": 1}}},
        {"type": "pong"},
        {"type": "next", "payload": {"data": {"counter": 2}}},
        {"type": "complete"},
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }"
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1, 2]
    finally:
        await client.close()


async def test_subscribe_pong_before_connection_ack():
    captured: dict[str, Any] = {}
    app = _make_ws_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
            {"type": "complete"},
        ],
        pre_ack_messages=[{"type": "pong"}],
        captured=captured,
    )

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }"
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1]
    finally:
        await client.close()


async def test_subscribe_next_without_payload_key():
    app = _make_ws_app([
        {"type": "next"},
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="No data in response"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_pre_ack_messages_exhaustion():
    app = _make_ws_app(
        [],
        pre_ack_messages=[{"type": "pong"}] * 17,
    )

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="No connection_ack after 16 messages"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_unsupported_url_scheme():
    client = GQLClient(base_url="ftp://testserver/graphql", target_app=None)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            ValueError, match="Unsupported URL scheme for WebSocket subscription: ftp"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_next_with_data_and_errors():
    app = _make_ws_app([
        {
            "type": "next",
            "payload": {
                "data": {"counter": 1},
                "errors": [{"message": "partial failure"}],
            },
        },
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="partial failure"):
            await consume()
    finally:
        await client.close()


def _make_ws_disconnect_app(
    messages_before_close: list[dict[str, Any]],
    close_code: int,
    close_reason: str = "",
) -> Any:
    async def app(
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope.get("subprotocols", [])
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = json.loads((await receive())["text"])
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.send",
            "text": json.dumps({"type": "connection_ack"}),
        })
        subscribe_msg = json.loads((await receive())["text"])
        assert subscribe_msg["type"] == "subscribe"
        sub_id = subscribe_msg["id"]

        for msg in messages_before_close:
            await send({
                "type": "websocket.send",
                "text": json.dumps({"id": sub_id, **msg}),
            })

        await send({
            "type": "websocket.close",
            "code": close_code,
            "reason": close_reason,
        })

    return app


async def test_subscribe_normal_closure_during_messages():
    app = _make_ws_disconnect_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
            {"type": "next", "payload": {"data": {"counter": 2}}},
        ],
        close_code=1000,
    )

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        async with client.subscribe(
            _CounterResult, "subscription { counter }"
        ) as stream:
            results = [item.counter async for item in stream]
        assert results == [1, 2]
    finally:
        await client.close()


async def test_subscribe_abnormal_disconnect_during_messages():
    app = _make_ws_disconnect_app(
        [
            {"type": "next", "payload": {"data": {"counter": 1}}},
        ],
        close_code=1011,
        close_reason="Internal Error",
    )

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:
        results: list[int] = []

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for item in stream:
                    results.append(item.counter)  # noqa: PERF401

        with pytest.raises(
            GraphQLResponseError, match="disconnected with code 1011: Internal Error"
        ):
            await consume()

        assert results == [1]
    finally:
        await client.close()


async def test_subscribe_disconnect_before_ack_with_reason():
    async def app(
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope.get("subprotocols", [])
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = json.loads((await receive())["text"])
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.close",
            "code": 4401,
            "reason": "Unauthorized",
        })

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError,
            match="before connection_ack with code 4401: Unauthorized",
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_invalid_json_during_messages():
    async def app(
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope.get("subprotocols", [])
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = json.loads((await receive())["text"])
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.send",
            "text": json.dumps({"type": "connection_ack"}),
        })
        subscribe_msg = json.loads((await receive())["text"])
        assert subscribe_msg["type"] == "subscribe"
        await send({
            "type": "websocket.send",
            "text": "not valid json{{{",
        })
        await receive()

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="Server sent invalid JSON"):
            await consume()
    finally:
        await client.close()


async def test_subscribe_invalid_json_during_handshake():
    async def app(
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": b"ok"})
            return

        assert scope["type"] == "websocket"
        subprotocols = scope.get("subprotocols", [])
        connect_event = await receive()
        assert connect_event["type"] == "websocket.connect"
        await send({
            "type": "websocket.accept",
            "subprotocol": "graphql-transport-ws"
            if "graphql-transport-ws" in subprotocols
            else None,
        })
        init_msg = json.loads((await receive())["text"])
        assert init_msg["type"] == "connection_init"
        await send({
            "type": "websocket.send",
            "text": "<<<broken>>>",
        })
        await receive()

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(
            GraphQLResponseError, match="Server sent invalid JSON during handshake"
        ):
            await consume()
    finally:
        await client.close()


async def test_subscribe_validation_error():
    app = _make_ws_app([
        {"type": "next", "payload": {"data": {"counter": "not_an_int"}}},
    ])

    client = GQLClient(base_url="http://testserver/graphql", target_app=app)
    try:

        async def consume():
            async with client.subscribe(
                _CounterResult, "subscription { counter }"
            ) as stream:
                async for _ in stream:
                    pass

        with pytest.raises(GraphQLResponseError, match="Invalid data in response"):
            await consume()
    finally:
        await client.close()
