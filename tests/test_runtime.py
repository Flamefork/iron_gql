import datetime
import io
import json
from decimal import Decimal
from typing import TypedDict
from typing import cast

import httpx2
import pytest
from graphql import GraphQLResolveInfo
from pydantic import TypeAdapter
from pytest_httpserver import HTTPServer
from werkzeug import Request
from werkzeug import Response

from iron_gql import FileVar
from iron_gql import GraphQLResponseError
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import serialize_variables
from tests.conftest import generated_package
from tests.conftest import gql_server
from tests.conftest import use_package_client

PACKAGE = "runtime"

# One package for the whole file. Sixteen used to stand here, six of them
# spelling the identical `query Ping { ping }` over the identical schema: what
# the tests below vary is the *transport* -- a base url without a slash, a
# redirect, a malformed body, a response with no data -- and none of that is a
# property of the generated package at all. Pointing one package's client at a
# different server is the whole difference, so one package it is.
generated_package(
    PACKAGE,
    schema="""
    scalar Upload
    scalar DateTime

    type Query {
        ping: String!
        fail: String!
        ok: String!
        user(id: ID!): User
        numbers1: [Int]!
        numbers2: [Int!]
        posts(limit: Int = 5): [Int!]!
        events(since: DateTime!): [Event!]!
        search(criteria: SearchCriteria!): String!
    }

    type Mutation {
        updateUser(input: UpdateUserInput!): User
        uploadFile(file: Upload!, label: String): String!
    }

    type User {
        id: ID!
        name: String!
    }

    type Event {
        name: String!
        startedAt: DateTime!
    }

    input UpdateUserInput {
        id: ID!
        name: String!
    }

    input SearchCriteria @oneOf {
        name: String
        email: String
    }
    """,
    queries='''
    from tests.generated.runtime.gql.api import api_gql

    ping = api_gql("query Ping { ping }")
    fail = api_gql("query Fail { fail }")
    ok = api_gql("query Ok { ok }")

    get_user = api_gql(
        """
        query GetUser($id: ID!) {
            user(id: $id) {
                id
                name
            }
        }
        """
    )

    update_user = api_gql(
        """
        mutation UpdateUser($input: UpdateUserInput!) {
            updateUser(input: $input) {
                id
                name
            }
        }
        """
    )

    numbers = api_gql(
        """
        query Numbers {
            numbers1
            numbers2
        }
        """
    )

    get_posts = api_gql(
        """
        query GetPosts($limit: Int = 5) {
            posts(limit: $limit)
        }
        """
    )

    get_events = api_gql(
        """
        query GetEvents($since: DateTime!) {
            events(since: $since) {
                name
                startedAt
            }
        }
        """
    )

    search = api_gql(
        """
        query Search($criteria: SearchCriteria!) {
            search(criteria: $criteria)
        }
        """
    )

    upload_file = api_gql(
        """
        mutation UploadFile($file: Upload!, $label: String!) {
            uploadFile(file: $file, label: $label)
        }
        """
    )

    upload = api_gql(
        """
        mutation Upload($file: Upload!) {
            uploadFile(file: $file)
        }
        """
    )
    ''',
)

from tests.generated.runtime import queries
from tests.generated.runtime.gql import api
from tests.generated.runtime.gql.api import SearchCriteriaEmail
from tests.generated.runtime.gql.api import SearchCriteriaName
from tests.generated.runtime.gql.api import UpdateUserInput


class UploadOperations(TypedDict):
    variables: dict[str, object]


class UploadCapture(TypedDict):
    operations: UploadOperations
    map: dict[str, list[str]]
    file_content: str
    file_name: str | None


class ContentTypeCapture(TypedDict):
    content_type: str | None
    file_name: str | None


# json.loads returns Any; validate the multipart fields into real types instead
OPERATIONS_ADAPTER = TypeAdapter(UploadOperations)
MAP_ADAPTER = TypeAdapter(dict[str, list[str]])


async def test_generate_and_execute_queries(httpserver: HTTPServer):
    state = {"user-1": "Graph"}

    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str] | None:
        name = state.get(id)
        if name is None:
            return None
        return {"id": id, "name": name}

    def resolve_update_user(
        _root: None, _info: GraphQLResolveInfo, *, input: dict[str, str]
    ) -> dict[str, str]:
        user_id = str(input["id"])
        state[user_id] = input["name"]
        return {"id": user_id, "name": input["name"]}

    async with gql_server(
        httpserver,
        "runtime",
        {
            "Query": {"user": resolve_user},
            "Mutation": {"updateUser": resolve_update_user},
        },
    ):
        read_query = queries.get_user.with_headers({"Authorization": "Bearer token"})
        initial = await read_query.execute(id="user-1")
        assert initial.user is not None
        assert initial.user.name == "Graph"

        mutation_input = UpdateUserInput(id="user-1", name="Bob")
        updated = await queries.update_user.execute(input=mutation_input)
        assert updated.update_user is not None
        assert updated.update_user.name == "Bob"
        refreshed = await queries.get_user.execute(id="user-1")
        assert refreshed.user is not None
        assert refreshed.user.name == "Bob"


async def test_list_allows_null_elements(httpserver: HTTPServer):
    call_count = 0

    def resolve_numbers1(_root: None, _info: GraphQLResolveInfo) -> list[int | None]:
        nonlocal call_count
        if call_count == 0:
            return [1, None]  # valid: [Int]! allows null elements
        return [1, 2]

    def resolve_numbers2(_root: None, _info: GraphQLResolveInfo) -> list[int | None]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [1, 2]  # valid: [Int!] doesn't allow null elements
        return [1, None]  # invalid: null in [Int!]

    async with gql_server(
        httpserver,
        "runtime",
        {"Query": {"numbers1": resolve_numbers1, "numbers2": resolve_numbers2}},
    ):
        response = await queries.numbers.execute()
        assert response.numbers_1 == [1, None]
        assert response.numbers_2 == [1, 2]

        with pytest.raises(GraphQLResponseError):
            await queries.numbers.execute()


async def test_variable_defaults_optional(httpserver: HTTPServer):
    def resolve_posts(
        _root: None, _info: GraphQLResolveInfo, *, limit: int = 5
    ) -> list[int]:
        return list(range(limit))

    async with gql_server(httpserver, "runtime", {"Query": {"posts": resolve_posts}}):
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


async def test_close(httpserver: HTTPServer):
    def resolve_ping(_root: None, _info: GraphQLResolveInfo) -> str:
        return "pong"

    async with gql_server(httpserver, "runtime", {"Query": {"ping": resolve_ping}}):
        await queries.ping.execute()

        client = cast("AsyncGQLClient", vars(api)["_client"])
        await client.close()
        await client.close()


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
        "input": {"avatar": FileVar(f1, filename="a.png"), "name": "Bob"},
        "cover": FileVar(f2, filename="c.jpg", content_type="image/jpeg"),
    }
    nulled, files = serialize_variables(variables)
    assert nulled == {"input": {"avatar": None, "name": "Bob"}, "cover": None}
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
    variables = {"name": "Bob", "age": 14}
    nulled, files = serialize_variables(variables)
    assert nulled == {"name": "Bob", "age": 14}
    assert files == {}


async def test_graphql_response_error(httpserver: HTTPServer):
    def graphql_error_handler(_request: Request) -> Response:
        return Response(
            json.dumps({"data": None, "errors": [{"message": "boom"}]}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        graphql_error_handler
    )
    async with use_package_client("runtime", httpserver.url_for("/graphql/")):
        with pytest.raises(GraphQLResponseError, match="boom"):
            await queries.fail.execute()


async def test_partial_errors_raise(httpserver: HTTPServer):
    def partial_error_handler(_request: Request) -> Response:
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
    async with use_package_client("runtime", httpserver.url_for("/graphql/")):
        with pytest.raises(GraphQLResponseError, match="partial failure"):
            await queries.ok.execute()


async def test_file_upload_multipart(httpserver: HTTPServer):
    captures: list[UploadCapture] = []

    def upload_handler(request: Request) -> Response:
        capture = UploadCapture(
            operations=OPERATIONS_ADAPTER.validate_json(request.form["operations"]),
            map=MAP_ADAPTER.validate_json(request.form["map"]),
            file_content=request.files["0"].stream.read().decode(),
            file_name=request.files["0"].filename,
        )
        captures.append(capture)
        return Response(
            json.dumps({"data": {"uploadFile": f"ok:{capture['file_content']}"}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        upload_handler
    )
    async with use_package_client("runtime", httpserver.url_for("/graphql/")):
        file_data = io.BytesIO(b"hello world")
        result = await queries.upload_file.execute(
            file=FileVar(file_data, filename="test.txt"), label="my-label"
        )

        assert result.upload_file == "ok:hello world"
        (received,) = captures
        assert received["operations"]["variables"]["file"] is None
        assert received["operations"]["variables"]["label"] == "my-label"
        assert received["map"] == {"0": ["variables.file"]}
        assert received["file_content"] == "hello world"
        assert received["file_name"] == "test.txt"


async def test_custom_scalar_in_variables_and_response(httpserver: HTTPServer):
    def resolve_events(
        _root: None, _info: GraphQLResolveInfo, *, since: str
    ) -> list[dict[str, str]]:
        return [{"name": "Launch", "startedAt": since}]

    async with gql_server(
        httpserver,
        "runtime",
        {"Query": {"events": resolve_events}},
    ):
        dt = datetime.datetime(2024, 1, 15, 10, 30, 0, tzinfo=datetime.UTC)
        result = await queries.get_events.execute(since=dt)
        assert result.events[0].name == "Launch"
        assert result.events[0].started_at == dt


async def test_base_url_without_trailing_slash_calls_exact_endpoint(
    httpserver: HTTPServer,
):
    def ping_handler(_request: Request) -> Response:
        return Response(
            json.dumps({"data": {"ping": "pong"}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql", method="POST").respond_with_handler(
        ping_handler
    )
    async with use_package_client("runtime", httpserver.url_for("/graphql")):
        result = await queries.ping.execute()
        assert result.ping == "pong"


async def test_file_upload_multipart_base_url_without_trailing_slash(
    httpserver: HTTPServer,
):
    captures: list[UploadCapture] = []

    def upload_handler(request: Request) -> Response:
        capture = UploadCapture(
            operations=OPERATIONS_ADAPTER.validate_json(request.form["operations"]),
            map=MAP_ADAPTER.validate_json(request.form["map"]),
            file_content=request.files["0"].stream.read().decode(),
            file_name=request.files["0"].filename,
        )
        captures.append(capture)
        return Response(
            json.dumps({"data": {"uploadFile": f"ok:{capture['file_content']}"}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql", method="POST").respond_with_handler(
        upload_handler
    )
    async with use_package_client("runtime", httpserver.url_for("/graphql")):
        file_data = io.BytesIO(b"hello world")
        result = await queries.upload_file.execute(
            file=FileVar(file_data, filename="test.txt"), label="my-label"
        )

        assert result.upload_file == "ok:hello world"
        (received,) = captures
        assert received["operations"]["variables"]["file"] is None
        assert received["operations"]["variables"]["label"] == "my-label"
        assert received["map"] == {"0": ["variables.file"]}
        assert received["file_content"] == "hello world"
        assert received["file_name"] == "test.txt"


async def test_redirect_response_raises_http_status_error(httpserver: HTTPServer):
    def redirect_handler(_request: Request) -> Response:
        return Response(
            "moved",
            status=307,
            headers={"Location": "/graphql/"},
            mimetype="text/plain",
        )

    httpserver.expect_request("/graphql", method="POST").respond_with_handler(
        redirect_handler
    )
    async with use_package_client("runtime", httpserver.url_for("/graphql")):
        with pytest.raises(
            httpx2.HTTPStatusError,
            match=r"Unexpected 3xx response \(307\) to /graphql/",
        ):
            await queries.ping.execute()


async def test_redirect_without_location_header(httpserver: HTTPServer):
    def redirect_handler(_request: Request) -> Response:
        return Response("moved", status=302, mimetype="text/plain")

    httpserver.expect_request("/graphql", method="POST").respond_with_handler(
        redirect_handler
    )
    async with use_package_client("runtime", httpserver.url_for("/graphql")):
        with pytest.raises(
            httpx2.HTTPStatusError,
            match=r"Unexpected 3xx response \(302\)",
        ):
            await queries.ping.execute()


async def test_no_data_in_response(httpserver: HTTPServer):
    def no_data_handler(_request: Request) -> Response:
        return Response(
            json.dumps({"extensions": {"tracing": True}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        no_data_handler
    )
    async with use_package_client("runtime", httpserver.url_for("/graphql/")):
        with pytest.raises(GraphQLResponseError, match="No data in response"):
            await queries.ping.execute()


async def test_malformed_response_body(httpserver: HTTPServer):
    def array_body_handler(_request: Request) -> Response:
        return Response(
            json.dumps([1, 2, 3]),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        array_body_handler
    )
    async with use_package_client("runtime", httpserver.url_for("/graphql/")):
        with pytest.raises(GraphQLResponseError, match="Malformed response body"):
            await queries.ping.execute()


async def test_one_of_input_runtime(httpserver: HTTPServer):
    def resolve_search(
        _root: None, _info: GraphQLResolveInfo, *, criteria: dict[str, str]
    ) -> str:
        if "name" in criteria:
            return f"found by name: {criteria['name']}"
        if "email" in criteria:
            return f"found by email: {criteria['email']}"
        return "not found"

    async with gql_server(httpserver, "runtime", {"Query": {"search": resolve_search}}):
        by_name = SearchCriteriaName(name="Alice")
        result = await queries.search.execute(criteria=by_name)
        assert result.search == "found by name: Alice"

        by_email = SearchCriteriaEmail(email="bob@example.com")
        result = await queries.search.execute(criteria=by_email)
        assert result.search == "found by email: bob@example.com"


async def test_file_upload_with_content_type(httpserver: HTTPServer):
    captures: list[ContentTypeCapture] = []

    def upload_handler(request: Request) -> Response:
        captures.append(
            ContentTypeCapture(
                content_type=request.files["0"].content_type,
                file_name=request.files["0"].filename,
            )
        )
        return Response(
            json.dumps({"data": {"uploadFile": "ok"}}),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        upload_handler
    )
    async with use_package_client("runtime", httpserver.url_for("/graphql/")):
        file_data = io.BytesIO(b"image data")
        result = await queries.upload.execute(
            file=FileVar(file_data, filename="photo.png", content_type="image/png")
        )
        assert result.upload_file == "ok"
        (received,) = captures
        assert received["content_type"] == "image/png"
        assert received["file_name"] == "photo.png"
