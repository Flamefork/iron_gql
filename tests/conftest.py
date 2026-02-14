import importlib
import json
import sys
import textwrap
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import ModuleType

import graphql
import pytest
from pydantic import alias_generators
from pytest_httpserver import HTTPServer
from werkzeug import Response

from iron_gql.generator import generate_gql_package

Resolver = Callable[..., object]
Resolvers = Mapping[str, Mapping[str, Resolver]]


def _build_schema(
    schema: str, resolvers: Resolvers | None = None
) -> graphql.GraphQLSchema:
    schema_obj = graphql.build_schema(schema)
    if not resolvers:
        return schema_obj
    for type_name, fields in resolvers.items():
        gql_type = schema_obj.get_type(type_name)
        assert isinstance(gql_type, graphql.GraphQLObjectType)  # noqa: S101
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


@dataclass(slots=True)
class ProjectBuilder:
    root: Path
    _monkeypatch: pytest.MonkeyPatch = field(repr=False)
    package: str = "sample_app"
    gql_pkg: str = "sample_app.gql.api"

    def write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")

    def clear_modules(self) -> None:
        for module_name in list(sys.modules):
            if module_name == self.package or module_name.startswith(
                f"{self.package}."
            ):
                sys.modules.pop(module_name, None)

    def prepare(
        self, *, schema: str, queries: str, base_url: str = "http://testserver/graphql/"
    ) -> None:
        self.write_file(self.root / "schema.graphql", schema)
        self.write_file(self.root / f"{self.package}/__init__.py", "")
        self.write_file(self.root / f"{self.package}/gql/__init__.py", "")
        self.write_file(
            self.root / f"{self.package}/settings.py",
            f"GRAPHQL_URL = {base_url!r}\n",
        )
        self.write_file(self.root / f"{self.package}/queries.py", queries)
        importlib.invalidate_caches()

    def generate(self) -> bool:
        return generate_gql_package(
            schema_path=self.root / "schema.graphql",
            package_full_name=self.gql_pkg,
            base_url_import=f"{self.package}.settings:GRAPHQL_URL",
            scalars={"ID": "builtins:str"},
            to_camel_fn_full_name="pydantic.alias_generators:to_camel",
            to_snake_fn=alias_generators.to_snake,
            src_path=self.root,
        )

    def import_api(self) -> ModuleType:
        self.clear_modules()
        return importlib.import_module(self.gql_pkg)

    def activate_workspace(self, path: Path) -> None:
        self._monkeypatch.chdir(path)
        self._monkeypatch.syspath_prepend(str(path))
        importlib.invalidate_caches()

    def generate_and_import(self) -> tuple[ModuleType, ModuleType]:
        changed = self.generate()
        assert changed is True  # noqa: S101
        importlib.invalidate_caches()
        self.clear_modules()
        api_module = importlib.import_module(self.gql_pkg)
        queries_module = importlib.import_module(f"{self.package}.queries")
        return api_module, queries_module

    @asynccontextmanager
    async def server(
        self,
        httpserver: HTTPServer,
        *,
        schema: str,
        queries: str,
        resolvers: Resolvers,
    ) -> AsyncIterator[tuple[ModuleType, ModuleType]]:
        schema_obj = _build_schema(schema, resolvers)
        base_url = _setup_httpserver(httpserver, schema_obj)
        self.prepare(schema=schema, queries=queries, base_url=base_url)
        api_module, queries_module = self.generate_and_import()
        try:
            yield api_module, queries_module
        finally:
            await api_module.API_CLIENT.close()


@pytest.fixture
def test_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectBuilder:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    importlib.invalidate_caches()
    return ProjectBuilder(root=tmp_path, _monkeypatch=monkeypatch)
