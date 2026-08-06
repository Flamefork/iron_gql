import importlib
import json
import socket
import sys
import textwrap
import threading
import time
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import asynccontextmanager
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import ModuleType

import graphql
import pydantic
import pytest
import uvicorn
from pydantic import alias_generators
from pytest_httpserver import HTTPServer
from werkzeug import Request
from werkzeug import Response

from iron_gql.codegen import GenerationMode
from iron_gql.codegen import generate_gql_package
from iron_gql.codegen.accessors import object_fields
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient

Resolver = Callable[..., object]
Resolvers = Mapping[str, Mapping[str, Resolver]]


class _GraphQLRequest(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore")
    query: str = ""
    variables: dict[str, object] | None = None
    operation_name: str | None = pydantic.Field(default=None, alias="operationName")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def generated_package(
    name: str, *, schema: str, queries: str, mode: GenerationMode = "async"
) -> None:
    """Generate a committed package under tests/generated/<name>/.

    Called at module level of a test file, before statically importing the
    generated code: the import always sees the fresh output, and the committed
    copy is what basedpyright checks and what PRs show as the codegen diff.
    CI catches a stale commit via a clean-worktree check after the test run.
    """
    root = Path(__file__).parent / "generated" / name
    _write_text(root / "schema.graphql", schema)
    _write_text(root / "__init__.py", "")
    _write_text(root / "settings.py", 'GRAPHQL_URL = "http://testserver/graphql/"\n')
    _write_text(root / "queries.py", queries)
    generate_gql_package(
        mode=mode,
        schema_path=root / "schema.graphql",
        src_path=root,
        package_full_name="gql.api",
        base_url_import=f"tests.generated.{name}.settings:GRAPHQL_URL",
        scalars={"ID": "builtins:str"},
        to_camel_fn_full_name="pydantic.alias_generators:to_camel",
        to_snake_fn=alias_generators.to_snake,
    )
    _write_text(root / "gql" / "__init__.py", "")


@asynccontextmanager
async def use_client(
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    base_url: str,
    target_app: ASGIApp | None = None,
) -> AsyncIterator[None]:
    """Point the committed generated package's API_CLIENT at base_url."""
    client = AsyncGQLClient(base_url=base_url, target_app=target_app)
    api_module = importlib.import_module(f"tests.generated.{package}.gql.api")
    monkeypatch.setattr(api_module, "API_CLIENT", client)
    try:
        yield
    finally:
        await client.close()


# `socket.getsockname` is typed as Any; validate the pair we know it returns
# for an IPv4 socket instead of casting.
_SOCKET_ADDRESS = pydantic.TypeAdapter(tuple[str, int])


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        _host, port = _SOCKET_ADDRESS.validate_python(probe.getsockname())
        return port


# Serves an ASGI app on a loopback port and yields its GraphQL base URL. The
# synchronous client has no in-process transport: httpx2's ASGITransport is
# async-only and WSGI cannot carry websockets. Sync tests therefore drive the
# same kind of fake app the async tests use, over a real socket.
@contextmanager
def live_asgi_server(app: ASGIApp) -> Iterator[str]:
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                msg = "uvicorn did not start within 10 seconds"
                raise RuntimeError(msg)
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}/graphql"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# The sync counterpart of `use_client`; the sync client takes no target_app,
# so its base_url always points at a real server.
@contextmanager
def use_sync_client(
    monkeypatch: pytest.MonkeyPatch, package: str, base_url: str
) -> Iterator[None]:
    client = GQLClient(base_url=base_url)
    api_module = importlib.import_module(f"tests.generated.{package}.gql.api")
    monkeypatch.setattr(api_module, "API_CLIENT", client)
    try:
        yield
    finally:
        client.close()


@contextmanager
def sync_gql_server(
    httpserver: HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    resolvers: Resolvers,
) -> Iterator[None]:
    root = Path(__file__).parent / "generated" / package
    schema_obj = build_schema((root / "schema.graphql").read_text(), resolvers)
    base_url = setup_httpserver(httpserver, schema_obj)
    with use_sync_client(monkeypatch, package, base_url):
        yield


@asynccontextmanager
async def gql_server(
    httpserver: HTTPServer,
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    resolvers: Resolvers,
) -> AsyncIterator[None]:
    """Serve the schema of a committed generated package with the given
    resolvers and point the package's API_CLIENT at the test server."""
    root = Path(__file__).parent / "generated" / package
    schema_obj = build_schema((root / "schema.graphql").read_text(), resolvers)
    base_url = setup_httpserver(httpserver, schema_obj)
    async with use_client(monkeypatch, package, base_url):
        yield


def build_schema(
    schema: str, resolvers: Resolvers | None = None
) -> graphql.GraphQLSchema:
    schema_obj = graphql.build_schema(schema)
    if not resolvers:
        return schema_obj
    for type_name, fields in resolvers.items():
        gql_type = schema_obj.get_type(type_name)
        assert isinstance(gql_type, graphql.GraphQLObjectType)  # noqa: S101
        for field_name, resolver in fields.items():
            object_fields(gql_type)[field_name].resolve = resolver
    return schema_obj


def setup_httpserver(httpserver: HTTPServer, schema_obj: graphql.GraphQLSchema) -> str:
    def graphql_handler(request: Request) -> Response:
        payload = _GraphQLRequest.model_validate(request.get_json(silent=True) or {})
        # graphql-core types `middleware` via unparameterized Tuple/List, so the
        # function type itself is partially unknown; its return type is fine.
        result = graphql.graphql_sync(  # pyright: ignore[reportUnknownMemberType]
            schema_obj,
            payload.query,
            variable_values=payload.variables,
            operation_name=payload.operation_name,
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

    def generate(
        self,
        to_camel_fn_full_name: str = "pydantic.alias_generators:to_camel",
        base_url_import: str | None = None,
        package_full_name: str | None = None,
        mode: GenerationMode = "async",
    ) -> bool:
        return generate_gql_package(
            mode=mode,
            schema_path=self.root / "schema.graphql",
            src_path=self.root,
            package_full_name=package_full_name or self.gql_pkg,
            base_url_import=base_url_import or f"{self.package}.settings:GRAPHQL_URL",
            scalars={"ID": "builtins:str"},
            to_camel_fn_full_name=to_camel_fn_full_name,
            to_snake_fn=alias_generators.to_snake,
        )

    def import_api(self) -> ModuleType:
        self.clear_modules()
        return importlib.import_module(self.gql_pkg)

    def activate_workspace(self, path: Path) -> None:
        self._monkeypatch.chdir(path)
        # pytest leaves `syspath_prepend`'s `path` parameter unannotated
        self._monkeypatch.syspath_prepend(str(path))  # pyright: ignore[reportUnknownMemberType]
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
        schema_obj = build_schema(schema, resolvers)
        base_url = setup_httpserver(httpserver, schema_obj)
        self.prepare(schema=schema, queries=queries, base_url=base_url)
        api_module, queries_module = self.generate_and_import()
        try:
            yield api_module, queries_module
        finally:
            # attributes of a dynamically imported module are Any
            await api_module.API_CLIENT.close()  # pyright: ignore[reportAny]


@pytest.fixture
def test_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectBuilder:
    # pytest leaves `syspath_prepend`'s `path` parameter unannotated
    monkeypatch.syspath_prepend(str(tmp_path))  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.chdir(tmp_path)
    importlib.invalidate_caches()
    return ProjectBuilder(root=tmp_path, _monkeypatch=monkeypatch)
