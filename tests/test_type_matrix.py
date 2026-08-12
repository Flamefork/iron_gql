"""Every kind of GraphQL type in every position it can be written in.

The package this generates replaces a shelf of per-feature fixtures, each of
which wrote a minimal schema covering the cells its own feature needed. Both
axes were covered many times over that way, and their crossings hardly at all
-- an enum reachable only through a bound fragment's variable was a blank cell
that no test addressed and the generator got wrong.

The generation-time checks are the post-conditions every package answers now
(`tests/corpus/generated_oracles`); what this file adds is the runtime half:
each cell's value goes to a real graphql-core server and comes back through
the generated model, so pydantic's idea of the type and graphql-core's have to
agree.
"""

import json
from collections.abc import Callable
from collections.abc import Coroutine
from typing import cast

import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from tests.conftest import Resolvers
from tests.conftest import generated_package
from tests.conftest import gql_server
from tests.corpus import type_matrix

PACKAGE = "type_matrix"

generated_package(
    PACKAGE, schema=type_matrix.schema(), queries=type_matrix.queries(PACKAGE)
)

from tests.generated.type_matrix import queries
from tests.generated.type_matrix.gql.api import EveryCellResult
from tests.generated.type_matrix.gql.api import Payload


def _spread_execute(
    execute: object,
) -> Callable[..., Coroutine[object, object, object]]:
    # The boundary the table crosses, narrowed once. The generated `execute`
    # gives every cell its own parameter, and a dictionary built from the axes
    # cannot be typed against twenty of them -- so the contract stated here is
    # the weakest one that is still checked: something callable that awaits to
    # a value. What came back is validated below.
    return cast("Callable[..., Coroutine[object, object, object]]", execute)


async def _execute_every_cell() -> EveryCellResult:
    result = await _spread_execute(queries.every_cell.execute)(
        **type_matrix.call_arguments()
    )
    assert isinstance(result, EveryCellResult)
    return result


def _payload() -> Payload:
    return Payload.model_validate(type_matrix.call_arguments())


def _echo(payload: object, arguments: dict[str, object]) -> dict[str, object]:
    # The server side of the round trip: hand back what arrived, so a value
    # that survived coercion in both directions is observable.
    seen = json.dumps({"payload": payload, "arguments": arguments}, sort_keys=True)
    return {"seen": seen, "size": "SMALL", "sizes": ["SMALL", "LARGE"]}


def _resolve_echo(
    _root: object, _info: GraphQLResolveInfo, payload: object, **arguments: object
) -> dict[str, object]:
    return _echo(payload, arguments)


def _resolve_labelled(
    _root: object, _info: GraphQLResolveInfo, **arguments: object
) -> dict[str, object]:
    return _echo({}, arguments)


def _resolve_tagged(
    _root: object, _info: GraphQLResolveInfo, **arguments: object
) -> str:
    return json.dumps(arguments, sort_keys=True)


RESOLVERS: Resolvers = {
    "Query": {"echo": _resolve_echo, "labelled": _resolve_labelled},
    "Echo": {"tagged": _resolve_tagged},
}


def _decoded(raw: str) -> dict[str, object]:
    return cast("dict[str, object]", json.loads(raw))


async def test_every_cell_survives_a_round_trip(httpserver: HTTPServer):
    async with gql_server(httpserver, PACKAGE, RESOLVERS):
        result = await _execute_every_cell()

    assert result.echo is not None
    seen = _decoded(result.echo.seen)
    # Both positions carry the same values, so one comparison covers the cells
    # as operation variables and as an input object's fields at once.
    assert seen["arguments"] == type_matrix.wire_values()
    assert seen["payload"] == type_matrix.wire_values()


async def test_schema_defaults_apply_when_a_cell_is_left_out(httpserver: HTTPServer):
    async with gql_server(httpserver, PACKAGE, RESOLVERS):
        result = await queries.defaults.execute()

    assert result.labelled is not None
    seen = _decoded(result.labelled.seen)
    assert seen["arguments"] == {"size": "SMALL", "term": "d"}


async def test_a_bound_fragments_own_variables_reach_the_server(
    httpserver: HTTPServer,
):
    # The cell that was blank: a fragment's variable is the one position whose
    # type reaches the module through no query variable and no input object --
    # `frag_size`'s enum type still has to be declared in the module even
    # though nothing binds `size_parts` at module level (it is a factory: its
    # own closure uses a variable, so only its generated `with_args` names
    # the enum, and only once applied does it become bindable at all).
    async with gql_server(httpserver, PACKAGE, RESOLVERS):
        applied = queries.size_parts.with_args(frag_size="LARGE", frag_term="t")
        bound = queries.slotted.bind(echo=applied)
        result = await bound.execute(payload=_payload())

    data = applied.read(result.echo)
    assert data is not None
    assert _decoded(data.tagged) == {"size": "LARGE", "term": "t"}


@pytest.mark.parametrize(
    "cell", type_matrix.CELLS, ids=[cell.name for cell in type_matrix.CELLS]
)
def test_every_cell_is_a_field_of_the_generated_input_model(cell: type_matrix.Cell):
    # The table is only a table if the generator really produced every cell:
    # a schema that quietly lost a field would still round-trip the ones it
    # kept, and the comparison above would pass on a smaller dictionary.
    missing = f"{cell.kind.name} {cell.typ} missing from Payload"
    assert cell.python in Payload.model_fields, missing
