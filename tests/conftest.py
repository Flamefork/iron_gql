import importlib
import json
import os
import re
import subprocess
import sys
import textwrap
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
from typing import Any
from typing import cast

import graphql
import pydantic
import pytest
from pydantic import alias_generators
from pytest_httpserver import HTTPServer
from werkzeug import Request
from werkzeug import Response

from iron_gql import GraphQLResponseError
from iron_gql.codegen import GenerationMode
from iron_gql.codegen import RenderedModule
from iron_gql.codegen import render_gql_module
from iron_gql.codegen.accessors import object_fields
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.runtime import SubscriptionProtocol
from iron_gql.slots import GQLFragment
from iron_gql.slots import GQLSlotNode
from iron_gql.testing import accept_graphql_ws
from iron_gql.testing import use_client
from iron_gql.testing.server import async_live_asgi_server
from tests.corpus.generated_oracles import assert_documents_are_valid
from tests.corpus.generated_oracles import assert_each_module_imported_once
from tests.corpus.generated_oracles import assert_method_namespaces_are_closed
from tests.corpus.generated_oracles import assert_module_is_self_contained
from tests.corpus.graphql_over_http import ErrorsForm

type Resolver = Callable[..., object]
type Resolvers = Mapping[str, Mapping[str, Resolver]]


# Reads a slot node whose offered-fragments phantom has been erased. A bare
# `GQLSlotNode` is `GQLSlotNode[Never]` (the phantom's declared default), and
# under contravariance every node is assignable to it -- so this signature
# accepts any node while `slot_data__`, which carries no phantom, still
# answers for the fragment. That is exactly the shape of a type-erased path in
# a real program (a node behind a widened annotation, one that arrived as
# `Any` from dynamic code, a generated module the caller forgot to
# regenerate), and it is the seam every test pinning the runtime wiring guard
# goes through -- `GQLFragment.read` itself cannot reach the guard, because
# its phantom rejects an unoffered fragment before the program runs.
def read_type_erased[TData: pydantic.BaseModel](
    fragment: GQLFragment[TData, Any], node: GQLSlotNode | None
) -> TData | None:
    if node is None:
        return None
    return node.slot_data__(fragment)


class GraphQLRequest(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore")
    query: str = ""
    variables: dict[str, object] | None = None
    operation_name: str | None = pydantic.Field(default=None, alias="operationName")


def build_schema(sdl: str, resolvers: Resolvers) -> graphql.GraphQLSchema:
    schema = graphql.build_schema(sdl)
    for type_name, fields in resolvers.items():
        gql_type = schema.get_type(type_name)
        assert isinstance(gql_type, graphql.GraphQLObjectType)  # noqa: S101
        for field_name, resolver in fields.items():
            object_fields(gql_type)[field_name].resolve = resolver
    return schema


# Минимальный типизированный срез схемы `basedpyright --outputjson`: его
# достаточно для проверок, а `Any` из `json.loads` не просачивается в тесты.
class DiagnosticPosition(pydantic.BaseModel):
    line: int


class DiagnosticRange(pydantic.BaseModel):
    start: DiagnosticPosition


class Diagnostic(pydantic.BaseModel):
    file: Path
    severity: str
    message: str
    range: DiagnosticRange
    rule: str | None = None


class DiagnosticSummary(pydantic.BaseModel):
    files_analyzed: int = pydantic.Field(alias="filesAnalyzed")


class BasedPyrightReport(pydantic.BaseModel):
    general_diagnostics: list[Diagnostic] = pydantic.Field(alias="generalDiagnostics")
    # Пустой список означает успех только после подтверждения, что файлы
    # действительно были прочитаны: несуществующий путь даёт тот же результат.
    summary: DiagnosticSummary


def basedpyright_report(*check_paths: Path) -> BasedPyrightReport:
    # Запускается из корня, чтобы применялся конфиг проекта. Ошибка разбора JSON
    # явно сообщает о расхождении схемы ответа, а не теряется внутри теста.
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "basedpyright",
            "--pythonpath",
            sys.executable,
            "--outputjson",
            *(str(check_path) for check_path in check_paths),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parent.parent,
    )
    try:
        return pydantic.TypeAdapter(BasedPyrightReport).validate_json(completed.stdout)
    except pydantic.ValidationError as exc:
        msg = (
            "JSON-ответ basedpyright больше не соответствует ожидаемой форме "
            f"(exit code {completed.returncode}): {exc}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        )
        pytest.fail(msg)


# How a subscription ends on a message whose `errors` entry has this corpus
# form, whatever the protocol: well-formed errors are raised as they came, a
# malformed entry makes the payload malformed, and none leaves the message
# without the errors it needed -- the message names each of the last two.
def errors_read_by_form(
    error: GraphQLResponseError, form: ErrorsForm, *, malformed: str, none: str
) -> bool:
    match form.kind:
        case "errors":
            return error.errors == form.value
        case "malformed":
            return str(error).startswith(malformed)
        case "none":
            return str(error).startswith(none)


def make_subscription_app(messages: list[dict[str, object]]) -> ASGIApp:
    # A graphql-ws server that acks the subscription, replays `messages` in
    # order and closes.
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        connection = await accept_graphql_ws(scope, receive, send)
        subscription = await connection.ack()
        for msg in messages:
            await subscription.send_message(msg)
        await connection.drain()

    return app


_BODY_CHUNK = pydantic.TypeAdapter(bytes)
_HEADER_PAIRS = pydantic.TypeAdapter(list[tuple[bytes, bytes]])
_JSON_OBJECT = pydantic.TypeAdapter(dict[str, object])


# The server end of one GraphQL over SSE request, as steps a fake drives
# itself: the request as received, then whatever response the test needs --
# a stream it writes event by event, or a single response in its place.
@dataclass
class SSETestRequest:
    scope: ASGIScope
    headers: dict[str, str]
    body: dict[str, object]
    receive: ASGIReceive
    send: ASGISend

    async def respond(self, status: int, content_type: str, body: bytes) -> None:
        await self.send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type.encode())],
        })
        await self.send({"type": "http.response.body", "body": body})

    async def open_stream(self) -> None:
        await self.send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        })

    async def write(self, chunk: bytes) -> None:
        await self.send({
            "type": "http.response.body",
            "body": chunk,
            "more_body": True,
        })

    async def next(self, payload: dict[str, object]) -> None:
        await self.write(f"event: next\ndata: {json.dumps(payload)}\n\n".encode())

    async def complete(self) -> None:
        await self.write(b"event: complete\ndata: \n\n")

    # Closes the response cleanly: what the stream said so far is all of it.
    async def end(self) -> None:
        await self.send({"type": "http.response.body", "body": b""})

    # Waits for the client to hang up and returns what the server saw: a fake
    # that returns earlier ends the response under the client.
    async def wait_disconnect(self) -> object:
        return (await self.receive())["type"]


async def accept_graphql_sse(
    scope: ASGIScope, receive: ASGIReceive, send: ASGISend
) -> SSETestRequest:
    body = b""
    more_body = True
    while more_body:
        event = await receive()
        body += _BODY_CHUNK.validate_python(event["body"])
        more_body = bool(event.get("more_body"))
    headers = {
        key.decode(): value.decode()
        for key, value in _HEADER_PAIRS.validate_python(scope["headers"])
    }
    return SSETestRequest(
        scope, headers, _JSON_OBJECT.validate_json(body), receive, send
    )


# The SSE counterpart of `make_subscription_app`: one `next` per payload, then
# `complete`.
def make_sse_subscription_app(payloads: list[dict[str, object]]) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        request = await accept_graphql_sse(scope, receive, send)
        await request.open_stream()
        for payload in payloads:
            await request.next(payload)
        await request.complete()
        await request.end()

    return app


# A subscription server for `payloads` in either protocol, as the arguments
# `use_package_client` takes: graphql-transport-ws is served in process,
# graphql-sse needs a socket.
@asynccontextmanager
async def subscription_server(
    protocol: SubscriptionProtocol, payloads: list[dict[str, object]]
) -> AsyncIterator[tuple[str, ASGIApp | None]]:
    match protocol:
        case "graphql-transport-ws":
            yield (
                "http://testserver/graphql",
                make_subscription_app([
                    *({"type": "next", "payload": payload} for payload in payloads),
                    {"type": "complete"},
                ]),
            )
        case "graphql-sse":
            async with async_live_asgi_server(
                make_sse_subscription_app(payloads)
            ) as base_url:
                yield base_url, None


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def readme_fenced_blocks() -> list[str]:
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    _, _, after = readme.partition("\n## Fragment Slots\n")
    section, _, _ = after.partition("\n## Customization Hooks\n")
    return [
        match.group(1).rstrip("\n")
        for match in re.finditer(r"```python\n(.*?)```", section, re.DOTALL)
    ]


# What a fixture's schema gets unless it asks for more. A scalar mapping is
# rendered as an unconditional import into the module that receives it, so a
# mapping every fixture carries is a line in every committed module -- the
# schemas that need `Upload` pass it themselves (see `WIRE_SHAPE_SCHEMA`).
DEFAULT_SCALARS: Mapping[str, str] = {"ID": "builtins:str"}
UPLOAD_SCALARS: Mapping[str, str] = {
    **DEFAULT_SCALARS,
    "Upload": "iron_gql.runtime:FileVar",
}


def generated_package(
    name: str,
    *,
    schema: str,
    queries: str,
    mode: GenerationMode = "async",
    scalars: Mapping[str, str] = DEFAULT_SCALARS,
    settings: str = 'GRAPHQL_URL = "http://testserver/graphql/"\n',
    model_base_import: str = "pydantic:BaseModel",
) -> None:
    """Generate a committed package under tests/generated/<name>/.

    Called at module level of a test file, before statically importing the
    generated code: the import always sees the fresh output, and the committed
    copy is what basedpyright checks and what PRs show as the codegen diff.
    CI catches a stale commit via a clean-worktree check after the test run.
    """
    root = _package_root(name)
    write_text(root / "schema.graphql", schema)
    write_text(root / "__init__.py", "")
    write_text(root / "settings.py", settings)
    write_text(root / "queries.py", queries)
    render_gql_module(
        mode=mode,
        schema_path=root / "schema.graphql",
        src_path=root,
        module_full_name="gql.api",
        base_url_import=f"tests.generated.{name}.settings:GRAPHQL_URL",
        subscription_protocol="graphql-sse",
        scalars=dict(scalars),
        to_camel_fn_full_name="pydantic.alias_generators:to_camel",
        to_snake_fn=alias_generators.to_snake,
        model_base_import=model_base_import,
    ).write()
    write_text(root / "gql" / "__init__.py", "")
    _check_generated(_generated_api_module(name), root / "schema.graphql")


def _check_generated(module: ModuleType, schema_path: Path) -> None:
    # The post-conditions every generated module answers. Here rather than in
    # a test of their own so no module can be generated without them: the
    # modules a check is applied to by hand are the modules someone thought
    # of, and the gaps are the ones nobody did.
    assert_module_is_self_contained(module)
    assert_method_namespaces_are_closed(module)
    assert_each_module_imported_once(module)
    schema = graphql.build_schema(schema_path.read_text(encoding="utf-8"))
    assert_documents_are_valid(module, schema)


def _generated_api_module(package: str) -> ModuleType:
    return importlib.import_module(f"tests.generated.{package}.gql.api")


# Every path into a committed package goes through here, so moving
# `tests/generated/` is one edit rather than one per test that reads a fixture.
def _package_root(package: str) -> Path:
    return Path(__file__).parent / "generated" / package


def _package_schema(package: str) -> Path:
    return _package_root(package) / "schema.graphql"


def generated_source(package: str) -> str:
    # The committed generated module as it sits on disk: what basedpyright
    # checks and what a PR shows as the codegen diff.
    return (_package_root(package) / "gql" / "api.py").read_text(encoding="utf-8")


def generated_queries_path(package: str) -> Path:
    # The fixture's own `queries.py` — a developer-facing call site, handed to
    # basedpyright as the reproduction of what a user would write.
    return _package_root(package) / "queries.py"


@asynccontextmanager
async def use_package_client(
    package: str,
    base_url: str,
    target_app: ASGIApp | None = None,
    subscription_protocol: SubscriptionProtocol = "graphql-sse",
) -> AsyncIterator[None]:
    """Point the committed generated package's client at base_url."""
    client = AsyncGQLClient(
        base_url=base_url,
        subscription_protocol=subscription_protocol,
        target_app=target_app,
    )
    async with use_client(_generated_api_module(package), client):
        yield


@contextmanager
def use_sync_package_client(
    package: str,
    base_url: str,
    subscription_protocol: SubscriptionProtocol = "graphql-sse",
) -> Iterator[None]:
    """The sync counterpart of `use_package_client`; the sync client takes no
    target_app, so its base_url always points at a real server."""
    client = GQLClient(base_url=base_url, subscription_protocol=subscription_protocol)
    with use_client(_generated_api_module(package), client):
        yield


@contextmanager
def sync_gql_server(
    httpserver: HTTPServer, package: str, resolvers: Resolvers
) -> Iterator[None]:
    schema = build_schema(_package_schema(package).read_text(), resolvers)
    with use_sync_package_client(package, setup_httpserver(httpserver, schema)):
        yield


@asynccontextmanager
async def gql_server(
    httpserver: HTTPServer, package: str, resolvers: Resolvers
) -> AsyncIterator[None]:
    """Serve the schema of a committed generated package with the given
    resolvers and point the package's client at the test server."""
    schema = build_schema(_package_schema(package).read_text(), resolvers)
    async with use_package_client(package, setup_httpserver(httpserver, schema)):
        yield


def setup_httpserver(httpserver: HTTPServer, schema: graphql.GraphQLSchema) -> str:
    def graphql_handler(request: Request) -> Response:
        payload = GraphQLRequest.model_validate(request.get_json(silent=True) or {})
        # graphql-core types `middleware` via unparameterized Tuple/List, so the
        # function type itself is partially unknown; its return type is fine.
        result = graphql.graphql_sync(  # pyright: ignore[reportUnknownMemberType]
            schema,
            payload.query,
            variable_values=payload.variables,
            operation_name=payload.operation_name,
        )
        return Response(
            json.dumps(result.formatted), status=200, mimetype="application/json"
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
    gql_module: str = "sample_app.gql.api"

    def write_file(self, path: Path, content: str) -> None:
        write_text(path, content)

    def clear_import_state(self) -> None:
        for module_name in list(sys.modules):
            if module_name == self.package or module_name.startswith(
                f"{self.package}."
            ):
                del sys.modules[module_name]
        root_path = str(self.root)
        root_prefix = root_path + os.sep
        for path in list(sys.path_importer_cache):
            if path == root_path or path.startswith(root_prefix):
                del sys.path_importer_cache[path]

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

    def render(
        self,
        to_camel_fn_full_name: str = "pydantic.alias_generators:to_camel",
        base_url_import: str | None = None,
        module_full_name: str | None = None,
        mode: GenerationMode = "async",
        to_snake_fn: Callable[[str], str] = alias_generators.to_snake,
        subscription_protocol: SubscriptionProtocol = "graphql-sse",
        model_base_import: str = "pydantic:BaseModel",
        scalars: Mapping[str, str] = DEFAULT_SCALARS,
    ) -> RenderedModule:
        return render_gql_module(
            mode=mode,
            schema_path=self.root / "schema.graphql",
            src_path=self.root,
            module_full_name=module_full_name or self.gql_module,
            base_url_import=base_url_import or f"{self.package}.settings:GRAPHQL_URL",
            subscription_protocol=subscription_protocol,
            scalars=dict(scalars),
            to_camel_fn_full_name=to_camel_fn_full_name,
            to_snake_fn=to_snake_fn,
            model_base_import=model_base_import,
        )

    def generate(
        self,
        to_camel_fn_full_name: str = "pydantic.alias_generators:to_camel",
        base_url_import: str | None = None,
        module_full_name: str | None = None,
        mode: GenerationMode = "async",
        to_snake_fn: Callable[[str], str] = alias_generators.to_snake,
        subscription_protocol: SubscriptionProtocol = "graphql-sse",
        model_base_import: str = "pydantic:BaseModel",
        scalars: Mapping[str, str] = DEFAULT_SCALARS,
    ) -> None:
        self.render(
            to_camel_fn_full_name=to_camel_fn_full_name,
            base_url_import=base_url_import,
            module_full_name=module_full_name,
            mode=mode,
            to_snake_fn=to_snake_fn,
            subscription_protocol=subscription_protocol,
            model_base_import=model_base_import,
            scalars=scalars,
        ).write()

    def import_api(self) -> ModuleType:
        self.clear_import_state()
        return importlib.import_module(self.gql_module)

    def activate_workspace(self, path: Path) -> None:
        self._monkeypatch.chdir(path)
        # pytest leaves `syspath_prepend`'s `path` parameter unannotated
        self._monkeypatch.syspath_prepend(str(path))  # pyright: ignore[reportUnknownMemberType]

    def generate_and_import(
        self, subscription_protocol: SubscriptionProtocol = "graphql-sse"
    ) -> tuple[ModuleType, ModuleType]:
        self.generate(subscription_protocol=subscription_protocol)
        self.clear_import_state()
        api_module = importlib.import_module(self.gql_module)
        queries_module = importlib.import_module(f"{self.package}.queries")
        _check_generated(api_module, self.root / "schema.graphql")
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
        base_url = setup_httpserver(httpserver, build_schema(schema, resolvers))
        self.prepare(schema=schema, queries=queries, base_url=base_url)
        api_module, queries_module = self.generate_and_import()
        try:
            yield api_module, queries_module
        finally:
            client = cast("AsyncGQLClient", vars(api_module)["_client"])
            await client.close()


@pytest.fixture
def test_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ProjectBuilder]:
    # pytest leaves `syspath_prepend`'s `path` parameter unannotated
    monkeypatch.syspath_prepend(str(tmp_path))  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.chdir(tmp_path)
    builder = ProjectBuilder(root=tmp_path, _monkeypatch=monkeypatch)
    yield builder
    builder.clear_import_state()
