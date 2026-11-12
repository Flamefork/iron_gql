"""The generation envelope: every weird-but-valid input either gets a
diagnosed rejection or an importable module.

The corpus collects inputs surfaced by reviews and probes; the property is the
convergence contract — no input may crash codegen (AssertionError,
RecursionError) or produce a module that fails to import (SyntaxError,
NameError). Message wording is pinned by the targeted tests next to each
validation; here only the envelope itself is asserted.
"""

import inspect
import sys
import typing
from collections.abc import Awaitable
from collections.abc import Callable
from types import ModuleType
from typing import TypedDict

import pydantic
import pytest
from pytest_httpserver import HTTPServer

from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.conftest import Resolvers
from tests.corpus.name_collisions import NAME_COLLISIONS
from tests.corpus.name_collisions import NameCollision

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

        bound = s.bind(user=f)
        """,
        # The kind suffix makes the class `NoneFragment` (ADR 0004).
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
            status: QQueryResultChild
        }

        enum QQueryResultChild {
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

        bound = s.bind(user=f)
        """,
        "expect": "ok",
    },
    # A model named like a builtin the module reads would rebind it for every
    # annotation and body: `str` turned every `title: str | None` into the
    # model, `LookupError` the error `api_gql` raises. The models take their
    # next names instead.
    {
        "name": "types-named-like-builtins",
        "schema": """
        type Query {
            post: Post
        }

        type Post {
            id: ID!
            title: String
            text: str
            error: LookupError
        }

        type str {
            id: ID!
        }

        type LookupError {
            id: ID!
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { post { title text { id } error { id } } }")
        """,
        "expect": "ok",
    },
    # A field with a default binds its name in the class body, where pydantic
    # resolves the annotations: `item: item | None = None` read the default.
    # The model takes its next name instead.
    {
        "name": "type-named-like-a-defaulted-field",
        "schema": """
        type Query {
            post: Post
        }

        type Post {
            id: ID!
            item: item
        }

        type item {
            id: ID!
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql(
            "query Q($full: Boolean!) { post { id item @include(if: $full) { id } } }"
        )
        """,
        "expect": "ok",
    },
    # The same binding over a name nothing can move -- a builtin, a scalar's
    # module -- is rejected: `str: str | None = None` reads the default.
    {
        "name": "defaulted-field-named-like-its-builtin-type",
        "schema": USER_SCHEMA,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql(
            "query Q($full: Boolean!) { user { str: name @include(if: $full) } }"
        )
        """,
        "expect": "reject",
    },
    {
        "name": "defaulted-field-named-like-its-scalar-module",
        "schema": """
        scalar DateTime

        type Query {
            user: User
        }

        type User {
            seen: DateTime
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql(
            "query Q($full: Boolean!) { user { datetime: seen @include(if: $full) } }"
        )
        """,
        "expect": "reject",
    },
    {
        "name": "defaulted-field-named-like-its-enum",
        "schema": """
        type Query {
            user: User
        }

        type User {
            role: role
        }

        enum role {
            ADMIN
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql(
            "query Q($full: Boolean!) { user { role @include(if: $full) } }"
        )
        """,
        "expect": "reject",
    },
    # Pydantic resolves a class's own name ahead of its attributes, so a
    # recursive input field spelled like its type reads the class.
    {
        "name": "recursive-input-field-named-like-its-type",
        "schema": """
        type Query {
            users(filter: filter): [User!]!
        }

        input filter {
            filter: filter
            name: String
        }

        type User {
            id: ID!
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q($f: filter) { users(filter: $f) { id } }")
        """,
        "expect": "ok",
    },
    {
        "name": "optional-input-field-named-like-its-builtin-type",
        "schema": """
        type Query {
            users(filter: UserFilter): [User!]!
        }

        input UserFilter {
            str: String
        }

        type User {
            id: ID!
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q($f: UserFilter) { users(filter: $f) { id } }")
        """,
        "expect": "reject",
    },
    {
        "name": "enum-named-like-a-builtin",
        "schema": """
        type Query {
            user: User
        }

        type User {
            name: String
            kind: str
        }

        enum str {
            ADMIN
        }
        """,
        "queries": """
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { user { name kind } }")
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


SHADOWING_DIAGNOSES = {
    "defaulted-field-named-like-its-builtin-type": (
        "Field 'str' of model 'User' has a default while the class's annotations "
        "read 'str', so pydantic would resolve them to that default; choose a "
        "different alias or schema name"
    ),
    "defaulted-field-named-like-its-scalar-module": (
        "Field 'datetime' of model 'User' has a default while the class's "
        "annotations read 'datetime', so pydantic would resolve them to that "
        "default; choose a different alias or schema name"
    ),
    "defaulted-field-named-like-its-enum": (
        "Field 'role' of model 'User' has a default while the class's "
        "annotations read 'role', so pydantic would resolve them to that "
        "default; choose a different alias or schema name"
    ),
    "optional-input-field-named-like-its-builtin-type": (
        "Field 'str' of model 'UserFilter' has a default while the class's "
        "annotations read 'str', so pydantic would resolve them to that "
        "default; choose a different alias or schema name"
    ),
    "enum-named-like-a-builtin": (
        "Name 'str' is claimed by the builtin 'str' and by enum 'str'"
    ),
}


@pytest.mark.parametrize("name", sorted(SHADOWING_DIAGNOSES))
def test_a_name_shadowing_one_the_annotations_read_is_diagnosed(
    test_project: ProjectBuilder, name: str
):
    case = _case(name)
    test_project.prepare(schema=case["schema"], queries=case["queries"])
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    assert exc_info.value.errors == [SHADOWING_DIAGNOSES[name]]


@pytest.mark.parametrize(
    ("name", "taken", "shadowed"),
    [
        (
            "types-named-like-builtins",
            {"strWithId", "LookupErrorWithId"},
            {"str", "LookupError"},
        ),
        ("type-named-like-a-defaulted-field", {"itemWithId"}, {"item"}),
    ],
)
def test_a_model_named_like_a_name_the_module_reads_takes_its_next_name(
    test_project: ProjectBuilder, name: str, taken: set[str], shadowed: set[str]
):
    case = _case(name)
    test_project.prepare(schema=case["schema"], queries=case["queries"])
    api, _queries = test_project.generate_and_import()
    assert taken <= set(vars(api))
    assert not shadowed & set(vars(api))


def _case(name: str) -> dict[str, str]:
    return next(case for case in CASES if case["name"] == name)


async def _run_dup_spelling(queries: ModuleType) -> None:
    await queries.first.execute()  # pyright: ignore[reportAny]
    await queries.second.execute()  # pyright: ignore[reportAny]


async def _run_path_twins(queries: ModuleType) -> None:
    result = await queries.q.execute()  # pyright: ignore[reportAny]
    assert result.a_b.c.id == "1"  # pyright: ignore[reportAny]
    assert result.a.b_c.id == "2"  # pyright: ignore[reportAny]


async def _run_builtin_named_types(queries: ModuleType) -> None:
    result = await queries.q.execute()  # pyright: ignore[reportAny]
    assert result.post.title == "hello"  # pyright: ignore[reportAny]
    assert result.post.text.id == "2"  # pyright: ignore[reportAny]
    with pytest.raises(LookupError, match="unknown GraphQL statement"):
        _ = queries.api_gql("query Unknown { post { title } }")  # pyright: ignore[reportAny]


async def _run_defaulted_field_type(queries: ModuleType) -> None:
    result = await queries.q.execute(full=True)  # pyright: ignore[reportAny]
    assert result.post.item.id == "2"  # pyright: ignore[reportAny]


async def _run_recursive_input(queries: ModuleType) -> None:
    api = sys.modules["sample_app.gql.api"]
    nested = api.filter(filter=api.filter(name="inner"))  # pyright: ignore[reportAny]
    result = await queries.q.execute(f=nested)  # pyright: ignore[reportAny]
    assert result.users[0].id == "inner"  # pyright: ignore[reportAny]


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
        "name": "types-named-like-builtins",
        "case": _case("types-named-like-builtins"),
        "resolvers": {
            "Query": {
                "post": lambda *_: {
                    "id": "1",
                    "title": "hello",
                    "text": {"id": "2"},
                    "error": {"id": "3"},
                }
            }
        },
        "run": _run_builtin_named_types,
    },
    {
        "name": "type-named-like-a-defaulted-field",
        "case": _case("type-named-like-a-defaulted-field"),
        "resolvers": {"Query": {"post": lambda *_: {"id": "1", "item": {"id": "2"}}}},
        "run": _run_defaulted_field_type,
    },
    {
        "name": "recursive-input-field-named-like-its-type",
        "case": _case("recursive-input-field-named-like-its-type"),
        "resolvers": {
            "Query": {
                "users": lambda *_, **kwargs: ([
                    {"id": kwargs["filter"]["filter"]["name"]}
                ])
            }
        },
        "run": _run_recursive_input,
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


def _model_class(annotation: object) -> type[pydantic.BaseModel]:
    # Unwraps what the generator writes around a model reference: `| None`,
    # `list[...]` and `Annotated[...]`.
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return annotation
    args: tuple[object, ...] = typing.get_args(annotation)
    [inner] = [arg for arg in args if arg is not type(None)]
    return _model_class(inner)


def _model_at(
    queries: ModuleType, query: str, path: tuple[str, ...]
) -> type[pydantic.BaseModel]:
    operation = typing.cast("object", getattr(queries, query))
    execute = typing.cast("object", inspect.getattr_static(type(operation), "execute"))
    hints = typing.cast("dict[str, object]", typing.get_type_hints(execute))
    model = _model_class(hints["return"])
    for field in path:
        model = _model_class(model.model_fields[field].annotation)
    return model


@pytest.mark.parametrize(
    "case", NAME_COLLISIONS, ids=[case.name for case in NAME_COLLISIONS]
)
def test_shapes_with_coinciding_detailed_names_keep_their_own_classes(
    test_project: ProjectBuilder, case: NameCollision
):
    test_project.prepare(schema=case.schema, queries=case.queries)
    _api, queries = test_project.generate_and_import()
    models = {location: _model_at(queries, *location) for location in case.shapes}
    for location, fields in case.shapes.items():
        assert set(models[location].model_fields) == fields, location
    assert len(set(models.values())) == len(models), models
    for payload in case.payloads:
        result = _model_at(queries, payload.query, ())
        try:
            _ = result.model_validate(payload.data)
        except pydantic.ValidationError:
            assert not payload.accepted, payload
        else:
            assert payload.accepted, payload
