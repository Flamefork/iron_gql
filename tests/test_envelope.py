"""The generation envelope: every weird-but-valid input either gets a
diagnosed rejection or an importable module.

The corpus collects inputs surfaced by reviews and probes; the property is the
convergence contract — no input may crash codegen (AssertionError,
RecursionError) or produce a module that fails to import (SyntaxError,
NameError). Message wording is pinned by the targeted tests next to each
validation; here only the envelope itself is asserted.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from types import ModuleType
from typing import TypedDict

import pytest
from pytest_httpserver import HTTPServer

from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.conftest import Resolvers

USER_SCHEMA = """
type Query {
    user(id: ID): User
}

type User {
    id: ID!
    name: String
}
"""

CASES = [
    {
        "name": "duplicate-query-different-spelling",
        "schema": USER_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        first = api_gql("query Ping { user(id: \\"1\\") { id } }")
        second = api_gql('''
            query Ping { user(id: "1") { id } }
        ''')
        """,
        "expect": "ok",
    },
    {
        "name": "variable-named-class",
        "schema": USER_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q($class: ID) { user(id: $class) { id } }")
        """,
        "expect": "reject",
    },
    {
        "name": "field-aliased-class",
        "schema": USER_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { user { class: id } }")
        """,
        "expect": "reject",
    },
    {
        "name": "enum-sharing-model-raw-name",
        "schema": """
        type Query {
            child: Child
        }

        type Child {
            id: ID!
            status: QResultChild
        }

        enum QResultChild {
            ACTIVE
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query q { child { id status } }")
        """,
        "expect": "reject",
    },
    {
        "name": "path-twins-different-shapes",
        "schema": """
        type Query {
            aB: Outer
            a: Inner
        }

        type Outer {
            c: Thing
        }

        type Inner {
            bC: Thing2
        }

        type Thing {
            id: ID!
        }

        type Thing2 {
            id: ID!
            name: String
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query S { aB { c { id } } a { bC { id name } } }")
        """,
        "expect": "reject",
    },
    {
        "name": "path-twins-same-shape",
        "schema": """
        type Query {
            aB: Outer
            a: Inner
        }

        type Outer {
            c: Thing
        }

        type Inner {
            bC: Thing
        }

        type Thing {
            id: ID!
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query S { aB { c { id } } a { bC { id } } }")
        """,
        "expect": "ok",
    },
    {
        "name": "statically-empty-selection",
        "schema": USER_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { user { id @skip(if: true) } }")
        """,
        "expect": "reject",
    },
    {
        "name": "schema-type-named-none",
        "schema": """
        type Query {
            thing: None
        }

        type None {
            id: ID!
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { thing { id } }")
        """,
        "expect": "reject",
    },
    {
        "name": "enum-named-none",
        "schema": """
        type Query {
            user: User
        }

        type User {
            id: ID!
            status: None
        }

        enum None {
            ACTIVE
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { user { id status } }")
        """,
        "expect": "reject",
    },
]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_generation_rejects_or_yields_an_importable_module(
    test_project: ProjectBuilder, case: dict[str, str]
):
    test_project.prepare(schema=case["schema"], queries=case["queries"])
    rejection: Exception | None = None
    try:
        test_project.generate(base_url_import=case.get("base_url_import"))
    except (GraphQLGenerationError, ValueError) as exc:
        rejection = exc
    if rejection is not None:
        assert case["expect"] == "reject", f"unexpected rejection: {rejection}"
        assert str(rejection)
        return
    assert case["expect"] == "ok", "expected a rejection, generation succeeded"
    test_project.import_api()


def _case(name: str) -> dict[str, str]:
    return next(case for case in CASES if case["name"] == name)


async def _run_dup_spelling(queries: ModuleType) -> None:
    await queries.first.execute()  # pyright: ignore[reportAny]
    await queries.second.execute()  # pyright: ignore[reportAny]


async def _run_path_twins(queries: ModuleType) -> None:
    result = await queries.q.execute()  # pyright: ignore[reportAny]
    assert result.a_b.c.id == "1"  # pyright: ignore[reportAny]
    assert result.a.b_c.id == "2"  # pyright: ignore[reportAny]


class OracleCase(TypedDict):
    name: str
    case: dict[str, str]
    resolvers: Resolvers
    run: Callable[[ModuleType], Awaitable[None]]


ORACLE_CASES: list[OracleCase] = [
    {
        "name": "duplicate-query-different-spelling",
        "case": _case("duplicate-query-different-spelling"),
        "resolvers": {"Query": {"user": lambda *_, **__: {"id": "1"}}},
        "run": _run_dup_spelling,
    },
    {
        "name": "path-twins-same-shape",
        "case": _case("path-twins-same-shape"),
        "resolvers": {
            "Query": {
                "aB": lambda *_: {"c": {"id": "1"}},
                "a": lambda *_: {"bC": {"id": "2"}},
            }
        },
        "run": _run_path_twins,
    },
]


@pytest.mark.parametrize(
    "oracle", ORACLE_CASES, ids=[case["name"] for case in ORACLE_CASES]
)
async def test_accepted_inputs_execute_against_a_real_server(
    test_project: ProjectBuilder, httpserver: HTTPServer, oracle: OracleCase
):
    # The other half of the envelope: importing is not enough — the module's
    # execute must survive a response an actual server produces for the very
    # document the runtime sends.
    async with test_project.server(
        httpserver,
        schema=oracle["case"]["schema"],
        queries=oracle["case"]["queries"],
        resolvers=oracle["resolvers"],
    ) as (_api_module, queries_module):
        await oracle["run"](queries_module)
