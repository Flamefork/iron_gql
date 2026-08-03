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

SLOT_SCHEMA = """
type Query {
    item: Item
}

type Item {
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
        "name": "fragment-named-none",
        "schema": USER_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        f = api_gql("fragment none on User { id }")

        s = api_gql("query S { user(id: \\"1\\") @slot { __typename } }")
        """,
        "expect": "reject",
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
        "name": "base-url-symbol-slots",
        "schema": USER_SCHEMA,
        "base_url_import": "sample_app.queries:slots",
        "queries": """
        from sample_app.gql.api import api_gql

        slots = "http://testserver/graphql/"

        f = api_gql("fragment UserBits on User { id }")
        """,
        "expect": "reject",
    },
    {
        "name": "slot-alias-slot-name-meta",
        "schema": SLOT_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { item @slot { __typename slot_name__: name } }")
        """,
        "expect": "reject",
    },
    {
        "name": "slot-alias-mask-name",
        "schema": SLOT_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { item @slot { __typename mask__: name } }")
        """,
        "expect": "ok",
    },
    {
        "name": "conditional-slot-merged-with-plain",
        "schema": SLOT_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql('''
        query Q($flag: Boolean!) {
            item @slot @include(if: $flag) { __typename }
            item { __typename }
        }
        ''')
        """,
        "expect": "reject",
    },
    {
        "name": "complementary-conditional-slot",
        "schema": SLOT_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql('''
        query Q($flag: Boolean!) {
            item @slot @include(if: $flag) { __typename }
            item @skip(if: $flag) { __typename }
        }
        ''')
        """,
        "expect": "reject",
    },
    {
        "name": "distinct-variable-conditional-slot",
        "schema": SLOT_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql('''
        query Q($a: Boolean!, $b: Boolean!) {
            item @slot @include(if: $a) { __typename }
            item @include(if: $b) { __typename }
        }
        ''')
        """,
        "expect": "reject",
    },
    {
        "name": "all-slot-mixed-conditions",
        "schema": SLOT_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql('''
        query Q($flag: Boolean!) {
            item @slot @include(if: $flag) { __typename }
            item @slot @skip(if: $flag) { __typename }
        }
        ''')
        """,
        "expect": "reject",
    },
    {
        "name": "conditional-slot-alone",
        "schema": SLOT_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql('''
        query Q($flag: Boolean!) {
            item @slot @include(if: $flag) { __typename }
        }
        ''')
        """,
        "expect": "reject",
    },
    {
        "name": "statically-excluded-slot",
        "schema": SLOT_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql('''
        query Q {
            item { id }
            i2: item @slot @include(if: false) { __typename }
        }
        ''')
        """,
        "expect": "reject",
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
        "name": "marker-like-literal",
        "schema": """
        type Query {
            search(term: String): Item
            item: Item
        }

        type Item {
            id: ID!
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql('''
        query Q {
            search(term: "__slot__item") { id }
            item @slot { __typename }
        }
        ''')
        """,
        "expect": "ok",
    },
    {
        "name": "reserved-marker-token-literal",
        "schema": """
        type Query {
            search(term: String): Item
            item: Item
        }

        type Item {
            id: ID!
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql('''
        query Q {
            search(term: "__slot__0__") { id }
            item @slot { __typename }
        }
        ''')
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
    {
        "name": "single-letter-fragment",
        "schema": USER_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        f = api_gql("fragment F on User { id }")

        s = api_gql("query S { user(id: \\"1\\") @slot { __typename } }")
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


async def _run_marker_literal(queries: ModuleType) -> None:
    result = await queries.q.execute(item=[])  # pyright: ignore[reportAny]
    assert result.search is not None  # pyright: ignore[reportAny]


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
    {
        "name": "marker-like-literal",
        "case": _case("marker-like-literal"),
        "resolvers": {
            "Query": {
                "search": lambda *_, **__: {"id": "s"},
                "item": lambda *_: {"__typename": "Item"},
            }
        },
        "run": _run_marker_literal,
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


# Compatibility corpus: idioms that generated on the parent of the feature
# commit and must keep generating. "reject" is an acceptable envelope outcome
# for weird inputs, so over-rejection is invisible to the cases above — these
# pin the other direction.
NODE_SCHEMA = """
type Query {
    node: Node
    post: Post
}

interface Node {
    id: ID!
}

type Post implements Node {
    id: ID!
    title: String
}
"""

COMPAT_CASES = [
    {
        "name": "name-spread-interface-fragment-without-own-typename",
        "schema": NODE_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        f = api_gql("fragment NodeBits on Node { ... on Post { title } }")

        q = api_gql("query Q { node { __typename ...NodeBits } }")
        """,
        "expect": "ok",
    },
    {
        "name": "name-spread-union-fragment-without-own-typename",
        "schema": """
        type Query {
            attachment: Attachment
        }

        union Attachment = Image | Link

        type Image {
            url: String
        }

        type Link {
            href: String
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        f = api_gql("fragment AttachmentBits on Attachment { ... on Image { url } }")

        q = api_gql("query Q { attachment { __typename ...AttachmentBits } }")
        """,
        "expect": "ok",
    },
    {
        "name": "name-spread-fragment-statically-empty-on-its-own",
        "schema": NODE_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        f = api_gql("fragment Maybe on Post { title @skip(if: true) }")

        q = api_gql("query Q { post { id ...Maybe } }")
        """,
        "expect": "ok",
    },
    {
        "name": "bundle-statement-generates-and-passes-through",
        "schema": NODE_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        b = api_gql('''
        fragment A on Post { id }

        fragment B on Post { title }
        ''')
        """,
        "expect": "ok",
    },
]


@pytest.mark.parametrize(
    "case", COMPAT_CASES, ids=[case["name"] for case in COMPAT_CASES]
)
def test_previously_supported_idioms_keep_generating(
    test_project: ProjectBuilder, case: dict[str, str]
):
    test_project.prepare(schema=case["schema"], queries=case["queries"])
    test_project.generate()
    test_project.import_api()
