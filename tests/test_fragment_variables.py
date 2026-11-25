"""A fragment's variable from `with_args` to the server, at every mix of
positions of the corpus.

The server is graphql-core: it refuses a document it finds invalid, and what a
position receives is what its resolver is called with.
"""

import json
from collections.abc import Awaitable
from collections.abc import Callable
from types import ModuleType
from typing import Protocol
from typing import cast
from typing import get_type_hints

import graphql
import pytest
from pytest_httpserver import HTTPServer

from iron_gql.codegen import GraphQLGenerationError
from iron_gql.slots import Omitted
from tests.conftest import ProjectBuilder
from tests.conftest import Resolvers
from tests.corpus.fragment_variables import ACCEPTED
from tests.corpus.fragment_variables import CONFLICTING
from tests.corpus.fragment_variables import AcceptedVariable
from tests.corpus.fragment_variables import FragmentVariable
from tests.corpus.fragment_variables import Position


class _Factory(Protocol):
    @property
    def with_args(self) -> Callable[..., object]: ...


type _Fetch = Callable[..., Awaitable[list[str]]]

_OMITTABLE = [case for case in ACCEPTED if case.omittable]
_REQUIRED = [case for case in ACCEPTED if not case.omittable]

_every_case = pytest.mark.parametrize(
    "case", ACCEPTED, ids=[case.id for case in ACCEPTED]
)


def _queries(case: FragmentVariable) -> str:
    # `fetch` sends the fragment with the given arguments and returns what its
    # fields came back with, in document order.
    return f"""
from sample_app.gql.api import api_gql

get_item = api_gql("query GetItem {{ item @slot {{ __typename }} }}")

parts = api_gql({case.fragment!r})


async def fetch(**variables):
    applied = parts.with_args(**variables)
    result = await get_item.bind(item=applied).execute()
    return list(applied.read(result.item).model_dump().values())
"""


def _operation_queries(case: AcceptedVariable) -> str:
    # The same, with the fragment spread by name and the variable declared by
    # the operation.
    operation = f"query GetItem($size: {case.declared}) {{ item {{ ...Parts }} }}"
    return f"""
from sample_app.gql.api import api_gql

get_item = api_gql({operation!r})

parts = api_gql({case.fragment!r})


async def fetch(**variables):
    result = await get_item.execute(**variables)
    return list(result.item.model_dump().values())
"""


def _resolve(
    _root: object, info: graphql.GraphQLResolveInfo[object], **arguments: object
) -> str:
    declared = [
        graphql.print_ast(variable.type)
        for variable in info.operation.variable_definitions or ()
    ]
    return json.dumps([*declared, arguments])


def _resolvers(case: FragmentVariable) -> Resolvers:
    return {
        "Query": {"item": lambda *_: {"id": "1"}},
        "Item": {f"p{index}": _resolve for index in range(len(case.positions))},
    }


def _namespace(module: ModuleType) -> dict[str, object]:
    return cast("dict[str, object]", vars(module))


def _with_args(queries: ModuleType) -> Callable[..., object]:
    parts = cast("_Factory", _namespace(queries)["parts"])
    return parts.with_args


async def _received(queries: ModuleType, **variables: object) -> list[object]:
    fetch = cast("_Fetch", _namespace(queries)["fetch"])
    return [cast("object", json.loads(answer)) for answer in await fetch(**variables)]


def _expected(
    case: AcceptedVariable, value: Callable[[Position], object]
) -> list[object]:
    # One answer per position: the type the operation declares the variable
    # with, and the arguments the position is called with.
    values = [value(position) for position in case.positions]
    return [
        [
            case.declared,
            {"value": received}
            if position.place == "argument"
            else {"by": {"value": received}},
        ]
        for position, received in zip(case.positions, values, strict=True)
    ]


def _sample(case: AcceptedVariable) -> object:
    return [9] if case.declared.startswith("[") else 9


def test_the_corpus_holds_variables_that_can_and_cannot_be_omitted():
    assert _OMITTABLE
    assert _REQUIRED


@_every_case
async def test_a_value_of_a_fragment_variable_reaches_every_position(
    test_project: ProjectBuilder, httpserver: HTTPServer, case: AcceptedVariable
):
    async with test_project.server(
        httpserver,
        schema=case.schema,
        queries=_queries(case),
        resolvers=_resolvers(case),
    ) as (_api, queries):
        received = await _received(queries, size=_sample(case))

    assert received == _expected(case, lambda _: _sample(case))


@_every_case
async def test_an_operation_may_declare_the_variable_of_a_fragment_as_with_args_does(
    test_project: ProjectBuilder, httpserver: HTTPServer, case: AcceptedVariable
):
    async with test_project.server(
        httpserver,
        schema=case.schema,
        queries=_operation_queries(case),
        resolvers=_resolvers(case),
    ) as (_api, queries):
        received = await _received(queries, size=_sample(case))

    assert received == _expected(case, lambda _: _sample(case))


@pytest.mark.parametrize("case", _OMITTABLE, ids=[case.id for case in _OMITTABLE])
async def test_an_omitted_fragment_variable_takes_the_default_of_every_position(
    test_project: ProjectBuilder, httpserver: HTTPServer, case: AcceptedVariable
):
    async with test_project.server(
        httpserver,
        schema=case.schema,
        queries=_queries(case),
        resolvers=_resolvers(case),
    ) as (_api, queries):
        received = await _received(queries)

    assert received == _expected(
        case, lambda position: cast("object", json.loads(position.default or ""))
    )


@pytest.mark.parametrize("case", _REQUIRED, ids=[case.id for case in _REQUIRED])
def test_a_fragment_variable_without_a_default_somewhere_is_required(
    test_project: ProjectBuilder, case: AcceptedVariable
):
    test_project.prepare(schema=case.schema, queries=_queries(case))
    _api, queries = test_project.generate_and_import()

    with pytest.raises(TypeError, match="size"):
        _with_args(queries)()


@_every_case
def test_the_value_of_a_fragment_variable_is_of_the_type_every_position_accepts(
    test_project: ProjectBuilder, case: AcceptedVariable
):
    test_project.prepare(schema=case.schema, queries=_queries(case))
    _api, queries = test_project.generate_and_import()

    hints = cast("dict[str, object]", get_type_hints(_with_args(queries)))
    assert hints["size"] == (case.value | Omitted if case.omittable else case.value)


@pytest.mark.parametrize("case", CONFLICTING, ids=[case.id for case in CONFLICTING])
def test_positions_no_declaration_fits_are_rejected_with_the_types_of_the_schema(
    test_project: ProjectBuilder, case: FragmentVariable
):
    test_project.prepare(schema=case.schema, queries=_queries(case))

    with pytest.raises(GraphQLGenerationError) as exc:
        test_project.render()
    [first, second] = case.positions
    assert (
        f"variable $size is used as '{first.typ}' in fragment 'Parts' and as "
        f"'{second.typ}' in fragment 'Parts'; no GraphQL variable declaration type "
        "is allowed at every usage -- rename the variable at one of these usages"
    ) in str(exc.value)
