import io
import json

import httpx2
import pydantic
import pytest
from pydantic import TypeAdapter
from pytest_httpserver import HTTPServer
from werkzeug import Request
from werkzeug import Response

from iron_gql import FileVar
from iron_gql import GraphQLResponseError
from iron_gql.runtime import GQLClient


class _User(pydantic.BaseModel):
    name: str


class _UserResult(pydantic.BaseModel):
    user: _User


class _UploadResult(pydantic.BaseModel):
    upload_file: str = pydantic.Field(validation_alias="uploadFile")


_MAP_ADAPTER = TypeAdapter(dict[str, list[str]])
_PAYLOAD_ADAPTER = TypeAdapter(dict[str, object])

_GET_USER = "query GetUser($id: ID!) { user(id: $id) { name } }"


def _json_response(body: dict[str, object]) -> Response:
    return Response(json.dumps(body), status=200, mimetype="application/json")


def test_sync_query_sends_variables_and_headers(httpserver: HTTPServer):
    seen: list[tuple[dict[str, object], str, str]] = []

    def handler(request: Request) -> Response:
        seen.append((
            _PAYLOAD_ADAPTER.validate_json(request.get_data()),
            request.headers["X-Token"],
            request.headers["X-Default"],
        ))
        return _json_response({"data": {"user": {"name": "Alice"}}})

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(handler)
    client = GQLClient(
        base_url=httpserver.url_for("/graphql/"), headers={"X-Default": "on"}
    )
    try:
        result = client.query(
            _UserResult,
            _GET_USER,
            variables={"id": "1"},
            headers={"X-Token": "secret"},
        )
    finally:
        client.close()

    assert result.user.name == "Alice"
    (payload, token, default_header) = seen[0]
    assert payload["query"] == _GET_USER
    assert payload["variables"] == {"id": "1"}
    assert token == "secret"
    assert default_header == "on"


def test_sync_query_raises_graphql_errors(httpserver: HTTPServer):
    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        lambda _request: _json_response({"errors": [{"message": "boom"}]})
    )
    client = GQLClient(base_url=httpserver.url_for("/graphql/"))
    try:
        with pytest.raises(GraphQLResponseError, match="boom"):
            client.query(_UserResult, _GET_USER, variables={"id": "1"}, headers={})
    finally:
        client.close()


def test_sync_query_rejects_redirect(httpserver: HTTPServer):
    httpserver.expect_request("/graphql/", method="POST").respond_with_response(
        Response(status=302, headers={"Location": "https://elsewhere/graphql"})
    )
    client = GQLClient(base_url=httpserver.url_for("/graphql/"))
    try:
        with pytest.raises(httpx2.HTTPStatusError, match="https://elsewhere/graphql"):
            client.query(_UserResult, _GET_USER, variables={"id": "1"}, headers={})
    finally:
        client.close()


def test_sync_query_uploads_files(httpserver: HTTPServer):
    seen: list[tuple[dict[str, list[str]], str, str | None]] = []

    def handler(request: Request) -> Response:
        seen.append((
            _MAP_ADAPTER.validate_json(request.form["map"]),
            request.files["0"].stream.read().decode(),
            request.files["0"].filename,
        ))
        return _json_response({"data": {"uploadFile": "ok"}})

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(handler)
    client = GQLClient(base_url=httpserver.url_for("/graphql/"))
    try:
        result = client.query(
            _UploadResult,
            "mutation Upload($file: Upload!) { uploadFile(file: $file) }",
            variables={"file": FileVar(io.BytesIO(b"hello"), filename="a.txt")},
            headers={},
        )
    finally:
        client.close()

    assert result.upload_file == "ok"
    (file_map, content, filename) = seen[0]
    assert file_map == {"0": ["variables.file"]}
    assert content == "hello"
    assert filename == "a.txt"
