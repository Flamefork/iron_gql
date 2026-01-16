import importlib
import json
import os
import sys
import textwrap
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import graphql
from pydantic import alias_generators
from pytest_httpserver import HTTPServer
from werkzeug import Response

from iron_gql.generator import generate_gql_package

Resolver = Callable[..., object]
Resolvers = Mapping[str, Mapping[str, Resolver]]


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def prepare_workspace(tmp_path: Path, query_source: str, schema: str) -> None:
    write_file(tmp_path / "schema.graphql", schema)
    write_file(tmp_path / "sample_app/__init__.py", "")
    write_file(tmp_path / "sample_app/gql/__init__.py", "")
    write_file(
        tmp_path / "sample_app/settings.py",
        "GRAPHQL_URL = 'http://testserver/graphql/'\n",
    )
    write_file(tmp_path / "sample_app/queries.py", query_source)


def clear_sample_app_modules() -> None:
    for module_name in list(sys.modules):
        if module_name == "sample_app" or module_name.startswith("sample_app."):
            sys.modules.pop(module_name, None)


@contextmanager
def import_path(path: Path) -> Iterator[None]:
    sys.path.insert(0, str(path))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        sys.path.remove(str(path))
        importlib.invalidate_caches()


@contextmanager
def working_directory(path: Path) -> Iterator[None]:
    current = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(current)


@contextmanager
def sample_app_context(path: Path) -> Iterator[None]:
    with import_path(path), working_directory(path):
        yield


def generate_api(tmp_path: Path) -> bool:
    return generate_gql_package(
        schema_path=tmp_path / "schema.graphql",
        package_full_name="sample_app.gql.api",
        base_url_import="sample_app.settings:GRAPHQL_URL",
        scalars={"ID": "builtins:str"},
        to_camel_fn_full_name="pydantic.alias_generators:to_camel",
        to_snake_fn=alias_generators.to_snake,
        src_path=tmp_path,
    )


def generate_and_import(tmp_path: Path) -> tuple[ModuleType, ModuleType]:
    changed = generate_api(tmp_path)
    assert changed is True
    clear_sample_app_modules()
    api_module = importlib.import_module("sample_app.gql.api")
    queries_module = importlib.import_module("sample_app.queries")
    return api_module, queries_module


def build_schema(
    schema: str, resolvers: Resolvers | None = None
) -> graphql.GraphQLSchema:
    schema_obj = graphql.build_schema(schema)
    if not resolvers:
        return schema_obj
    for type_name, fields in resolvers.items():
        gql_type = schema_obj.get_type(type_name)
        assert isinstance(gql_type, graphql.GraphQLObjectType)
        for field_name, resolver in fields.items():
            gql_type.fields[field_name].resolve = resolver
    return schema_obj


def _setup_httpserver(httpserver: HTTPServer, schema_obj: graphql.GraphQLSchema) -> str:
    def graphql_handler(request):
        payload = request.get_json(silent=True) or {}
        result = graphql.graphql_sync(
            schema_obj,
            payload.get("query", ""),
            variable_values=payload.get("variables"),
            operation_name=payload.get("operationName"),
        )
        return Response(
            json.dumps(result.formatted),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        graphql_handler
    )
    return httpserver.url_for("/graphql/")


@contextmanager
def setup_test_server(
    tmp_path: Path,
    httpserver: HTTPServer,
    schema: str,
    query_source: str,
    resolvers: Resolvers,
) -> Iterator[tuple[ModuleType, ModuleType]]:
    prepare_workspace(tmp_path, query_source, schema)
    with sample_app_context(tmp_path):
        api_module, queries_module = generate_and_import(tmp_path)
        schema_obj = build_schema(schema, resolvers)
        api_module.API_CLIENT.base_url = _setup_httpserver(httpserver, schema_obj)
        yield api_module, queries_module
