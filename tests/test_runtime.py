import datetime
import io
import json
from decimal import Decimal

import httpx
import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Response

from iron_gql import FileVar
from iron_gql import GraphQLResponseError
from iron_gql.runtime import serialize_variables
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


def test_prepare_variables_handles_nested_structures():
    nested_list = {"x": [[1, 2], [3, 4]]}
    result, files = serialize_variables(nested_list)
    assert result == {"x": [[1, 2], [3, 4]]}
    assert files == {}

    nested_dict = {"a": {"b": 1}, "c": [1, 2]}
    result, files = serialize_variables(nested_dict)
    assert result == {"a": {"b": 1}, "c": [1, 2]}
    assert files == {}

    mixed = {"a": [1, {"b": 2}]}
    result, files = serialize_variables(mixed)
    assert result == {"a": [1, {"b": 2}]}
    assert files == {}


def test_prepare_variables_handles_custom_scalar_types():
    dt = datetime.datetime(2024, 1, 15, 10, 30, 0, tzinfo=datetime.UTC)
    d = datetime.date(2024, 6, 1)

    result, files = serialize_variables({
        "dt": dt,
        "d": d,
        "dec": Decimal("12.50"),
        "dates": [dt, d],
    })
    assert result == {
        "dt": "2024-01-15T10:30:00Z",
        "d": "2024-06-01",
        "dec": "12.50",
        "dates": ["2024-01-15T10:30:00Z", "2024-06-01"],
    }
    assert files == {}


def test_prepare_variables_detects_file_uploads():
    variables = {"name": "test", "file": FileVar(b"data", filename="f.txt")}
    nulled, files = serialize_variables(variables)
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


def test_prepare_variables_single_file():
    f = io.BytesIO(b"hello")
    variables = {"file": FileVar(f, filename="test.txt")}
    nulled, files = serialize_variables(variables)
    assert nulled == {"file": None}
    assert list(files.keys()) == ["variables.file"]
    assert files["variables.file"].f is f
    assert files["variables.file"].filename == "test.txt"


def test_prepare_variables_nested_files():
    f1 = io.BytesIO(b"one")
    f2 = io.BytesIO(b"two")
    variables = {
        "input": {"avatar": FileVar(f1, filename="a.png"), "name": "Morty"},
        "cover": FileVar(f2, filename="c.jpg", content_type="image/jpeg"),
    }
    nulled, files = serialize_variables(variables)
    assert nulled == {"input": {"avatar": None, "name": "Morty"}, "cover": None}
    assert "variables.input.avatar" in files
    assert "variables.cover" in files
    assert files["variables.input.avatar"].f is f1
    assert files["variables.cover"].content_type == "image/jpeg"


def test_prepare_variables_files_in_list():
    f1 = io.BytesIO(b"a")
    f2 = io.BytesIO(b"b")
    variables = {"files": [FileVar(f1), FileVar(f2)]}
    nulled, files = serialize_variables(variables)
    assert nulled == {"files": [None, None]}
    assert "variables.files.0" in files
    assert "variables.files.1" in files


def test_prepare_variables_no_files():
    variables = {"name": "Morty", "age": 14}
    nulled, files = serialize_variables(variables)
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
