"""Pydantic secrets in request variables reach the server as their values.

A secret masks itself in `repr` and in pydantic's dumps, which is what a
consumer maps a secret scalar to it for; the request is the one place the value
has to go out as it is. Every kind of secret is sent from every depth a
variable value can hold it at, and the server's copy of the variables is the
oracle.
"""

import json
from collections.abc import Callable
from collections.abc import Coroutine
from typing import Protocol
from typing import cast

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request
from werkzeug import Response

from tests.conftest import GraphQLRequest
from tests.conftest import generated_package
from tests.conftest import use_package_client
from tests.corpus import secrets
from tests.corpus.secrets import DEPTHS
from tests.corpus.secrets import KINDS
from tests.corpus.secrets import Depth
from tests.corpus.secrets import SecretKind

PACKAGE = "secret_variables"

generated_package(
    PACKAGE,
    schema=secrets.schema(),
    scalars=secrets.SCALARS,
    queries=secrets.queries(PACKAGE),
)

from tests.generated.secret_variables import queries
from tests.generated.secret_variables.gql.api import Vault

type _Execute = Callable[..., Coroutine[object, object, object]]


class _Operation(Protocol):
    execute: _Execute


def _execute_of(depth: Depth) -> _Execute:
    # The generated `execute` is typed per operation; the table calls it with
    # keyword arguments it builds, so the boundary is narrowed once.
    return cast("_Operation", vars(queries)[depth.operation]).execute


@pytest.mark.parametrize("kind", KINDS, ids=[kind.scalar for kind in KINDS])
@pytest.mark.parametrize("depth", DEPTHS, ids=[depth.name for depth in DEPTHS])
async def test_secret_reaches_the_server_as_its_value(
    httpserver: HTTPServer, depth: Depth, kind: SecretKind
):
    seen: list[dict[str, object]] = []

    def handler(request: Request) -> Response:
        payload = GraphQLRequest.model_validate_json(request.get_data())
        seen.append(payload.variables or {})
        body = {"data": {depth.field: True}}
        return Response(json.dumps(body), status=200, mimetype="application/json")

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(handler)
    async with use_package_client(PACKAGE, httpserver.url_for("/graphql/")):
        await _execute_of(depth)(**depth.call(kind, Vault.model_validate))

    (variables,) = seen
    for name, value in depth.wire(kind).items():
        assert variables[name] == value


@pytest.mark.parametrize("kind", KINDS, ids=[kind.scalar for kind in KINDS])
def test_input_model_keeps_its_secret_out_of_repr_and_dumps(kind: SecretKind):
    # The request unwraps the secret by itself; the model a caller holds stays
    # as pydantic made it, masked wherever it is shown or dumped.
    vault = Vault.model_validate({kind.name: kind.value})
    shown = (
        repr(vault),
        str(vault),
        vault.model_dump_json(),
        json.dumps(vault.model_dump(mode="json")),
    )
    for text in shown:
        assert str(kind.wire) not in text
