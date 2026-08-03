import importlib

import pytest
from graphql import GraphQLResolveInfo
from pydantic import alias_generators
from pytest_httpserver import HTTPServer

from iron_gql import runtime
from iron_gql.codegen import GraphQLGenerationError
from iron_gql.codegen import UnknownGQLTypeWarning
from iron_gql.codegen import generate_gql_package
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.conftest import gql_server

generated_package(
    "generation_nested_inputs",
    schema="""
    type Query {
        ping: Boolean
    }

    type Mutation {
        updateUser(input: UpdateUserInput!): Boolean
    }

    input UpdateUserInput {
        id: ID!
        address: AddressInput
    }

    input AddressInput {
        street: String!
    }
    """,
    queries='''
    from tests.generated.generation_nested_inputs.gql.api import api_gql

    update_user = api_gql(
        """
        mutation UpdateUser($input: UpdateUserInput!) {
            updateUser(input: $input)
        }
        """
    )
    ''',
)

generated_package(
    "generation_enum_variable",
    schema="""
    type Query {
        search(status: Status!): Boolean
    }

    enum Status {
        ACTIVE
        INACTIVE
    }
    """,
    queries='''
    from tests.generated.generation_enum_variable.gql.api import api_gql

    search = api_gql(
        """
        query Search($status: Status!) {
            search(status: $status)
        }
        """
    )
    ''',
)

generated_package(
    "generation_anonymous",
    schema="""
    type Query {
        ping: String
    }
    """,
    queries="""
    from tests.generated.generation_anonymous.gql.api import api_gql

    # Anonymous query
    q = api_gql("query { ping }")
    """,
)

from tests.generated.generation_anonymous import queries as anonymous_queries
from tests.generated.generation_enum_variable import queries as enum_variable_queries
from tests.generated.generation_nested_inputs.gql.api import AddressInput
from tests.generated.generation_nested_inputs.gql.api import UpdateUserInput


def test_generate_with_schema_outside_src(test_project: ProjectBuilder):
    workspace = test_project.root / "workspace"
    workspace.mkdir()
    schema_path = test_project.root / "schema.graphql"
    schema_path.write_text(
        """\
type Query {
    ping: String!
}
""",
        encoding="utf-8",
    )

    (workspace / "sample_app").mkdir(parents=True)
    test_project.write_file(workspace / "sample_app/__init__.py", "")
    test_project.write_file(workspace / "sample_app/gql/__init__.py", "")
    test_project.write_file(
        workspace / "sample_app/settings.py",
        "GRAPHQL_URL = 'http://testserver/graphql/'\n",
    )
    test_project.write_file(
        workspace / "sample_app/queries.py",
        """
from sample_app.gql.api import api_gql

ping = api_gql(
    '''
    query Ping {
        ping
    }
    '''
)
""",
    )

    test_project.activate_workspace(workspace)

    changed = generate_gql_package(
        schema_path=schema_path,
        package_full_name="sample_app.gql.api",
        base_url_import="sample_app.settings:GRAPHQL_URL",
        scalars={"ID": "builtins:str"},
        to_camel_fn_full_name="pydantic.alias_generators:to_camel",
        to_snake_fn=alias_generators.to_snake,
        src_path=workspace,
    )
    assert changed is True

    test_project.clear_modules()
    api_module = importlib.import_module("sample_app.gql.api")
    # attributes of a dynamically imported module are Any
    assert isinstance(api_module.API_CLIENT, runtime.GQLClient)  # pyright: ignore[reportAny]


def test_duplicate_operations_raise(test_project: ProjectBuilder):
    schema = """
        type Query {
            user(id: ID!): User
        }

        type User {
            id: ID!
            name: String
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        first_query = api_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    id
                }
            }
            '''
        )

        second_query = api_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    id
                    name
                }
            }
            '''
        )
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"^Cannot compile different GraphQL queries with same name",
    ):
        test_project.generate()


def test_nested_input_objects_missing():
    address = AddressInput(street="Main St")
    UpdateUserInput(id="u-1", address=address)


def test_invalid_query_reports_error(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: String
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        broken = api_gql(
            '''
            query Broken {
                missingField
            }
            '''
        )
        """,
    )

    with pytest.raises(GraphQLGenerationError, match="missingField"):
        test_project.generate()


def test_input_type_dependency_ordering(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            createOrder(input: OrderInput!): Boolean
        }

        input OrderInput {
            id: ID!
            item: ItemInput!
        }

        input ItemInput {
            sku: String!
            quantity: Int!
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        create_order = api_gql(
            '''
            mutation CreateOrder($input: OrderInput!) {
                createOrder(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    # attributes of a dynamically imported module are Any
    item = api.ItemInput(sku="ABC123", quantity=2)  # pyright: ignore[reportAny]
    api.OrderInput(id="o-1", item=item)  # pyright: ignore[reportAny]


def test_self_referential_input_type(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            createTree(root: TreeNode!): Boolean
        }

        input TreeNode {
            value: String!
            children: [TreeNode]
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        create_tree = api_gql(
            '''
            mutation CreateTree($root: TreeNode!) {
                createTree(root: $root)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    # attributes of a dynamically imported module are Any
    leaf = api.TreeNode(value="leaf")  # pyright: ignore[reportAny]
    parent = api.TreeNode(value="parent", children=[leaf])  # pyright: ignore[reportAny]
    assert parent.children[0].value == "leaf"  # pyright: ignore[reportAny]


def test_input_enums_and_defaults(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            update(input: UpdateInput!): Boolean
        }

        enum Status {
            ACTIVE
            INACTIVE
        }

        input ChildInput {
            code: String!
        }

        input UpdateInput {
            status: Status
            note: String
            child: ChildInput = { code: "X" }
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        update = api_gql(
            '''
            mutation Update($input: UpdateInput!) {
                update(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    # attributes of a dynamically imported module are Any
    update = api.UpdateInput(status="ACTIVE")  # pyright: ignore[reportAny]
    assert isinstance(update.child, api.ChildInput)  # pyright: ignore[reportAny]
    assert update.child.code == "X"  # pyright: ignore[reportAny]
    serialized = runtime.serialize_variables({"x": update})[0]["x"]  # pyright: ignore[reportAny]
    assert serialized == {"status": "ACTIVE"}


async def test_operation_variable_enum_variable_executes(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_search(_root: None, _info: GraphQLResolveInfo, *, status: str) -> bool:
        return status == "ACTIVE"

    async with gql_server(
        httpserver,
        monkeypatch,
        "generation_enum_variable",
        {"Query": {"search": resolve_search}},
    ):
        active = await enum_variable_queries.search.execute(status="ACTIVE")
        assert active.search is True

        inactive = await enum_variable_queries.search.execute(status="INACTIVE")
        assert inactive.search is False


def test_unknown_scalar_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            customValue: CustomScalar
        }

        scalar CustomScalar
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_value = api_gql(
            '''
            query GetValue {
                customValue
            }
            '''
        )
        """,
    )

    with pytest.warns(UnknownGQLTypeWarning, match="Unknown scalar type: CustomScalar"):
        changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    # attributes of a dynamically imported module are Any
    result = api.GetValueResult(custom_value={"raw": "value"})  # pyright: ignore[reportAny]
    assert result.custom_value == {"raw": "value"}  # pyright: ignore[reportAny]


def test_debug_artifacts_generation(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: String
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        q = api_gql("query Ping { ping }")
        """,
    )

    debug_dir = test_project.root / "debug_out"
    generate_gql_package(
        schema_path=test_project.root / "schema.graphql",
        package_full_name="sample_app.gql.api",
        base_url_import="sample_app.settings:GRAPHQL_URL",
        src_path=test_project.root,
        debug_path=debug_dir,
    )

    assert (debug_dir / "schema.graphql").exists()
    assert (debug_dir / "queries.gql").exists()
    assert (debug_dir / "queries.json").exists()
    assert (debug_dir / "schema.json").exists()
    assert (debug_dir / "out.json").exists()


def test_introspection_query(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: String
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        q = api_gql(
            '''
            query Introspection {
                __schema {
                    types {
                        name
                        kind
                    }
                }
                __type(name: "Query") {
                    name
                }
            }
            '''
        )
        """,
    )
    assert test_project.generate() is True


def test_default_scalars_and_nested_list_input(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping(input: ComplexInput): String
        }

        input ComplexInput {
            matrix: [[Int]]
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        q = api_gql(
            '''
            query Ping($input: ComplexInput) {
                ping(input: $input)
            }
            '''
        )
        """,
    )

    # helper to test scalars=None path involved in default args
    # generate_gql_package has scalars=None default.
    generate_gql_package(
        schema_path=test_project.root / "schema.graphql",
        package_full_name="sample_app.gql.api",
        base_url_import="sample_app.settings:GRAPHQL_URL",
        src_path=test_project.root,
        # scalars argument omitted to test default None -> {}
    )

    api = test_project.import_api()
    variables, files = runtime.serialize_variables({
        # attributes of a dynamically imported module are Any
        "input": api.ComplexInput(matrix=[[1, None], None])  # pyright: ignore[reportAny]
    })
    assert variables == {"input": {"matrix": [[1, None], None]}}
    assert files == {}


async def test_anonymous_query_generation(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_ping(_root: None, _info: GraphQLResolveInfo) -> str:
        return "pong"

    async with gql_server(
        httpserver,
        monkeypatch,
        "generation_anonymous",
        {"Query": {"ping": resolve_ping}},
    ):
        result = await anonymous_queries.q.execute()
        assert result.ping == "pong"


async def test_list_variable_argument(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            users(ids: [ID]): [User]
        }
        type User {
            id: ID
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        get_users = api_gql(
            '''
            query GetUsers($ids: [ID]) {
                users(ids: $ids) {
                    id
                }
            }
            '''
        )
        """,
    )

    def resolve_users(
        _root: None, _info: GraphQLResolveInfo, *, ids: list[str] | None = None
    ) -> list[dict[str, str]]:
        if ids is None:
            return []
        return [{"id": id_value} for id_value in ids]

    async with test_project.server(
        httpserver,
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        get_users = api_gql(
            '''
            query GetUsers($ids: [ID]) {
                users(ids: $ids) {
                    id
                }
            }
            '''
        )
        """,
        resolvers={"Query": {"users": resolve_users}},
    ) as (_, queries):
        # attributes of a dynamically imported module are Any
        result = await queries.get_users.execute(ids=["u-1", "u-2"])  # pyright: ignore[reportAny]
        assert [user.id for user in result.users] == ["u-1", "u-2"]  # pyright: ignore[reportAny]


def test_regeneration_is_idempotent(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: String
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        q = api_gql("query Ping { ping }")
        """,
    )

    assert test_project.generate() is True
    assert test_project.generate() is False


def test_no_queries_generates_module(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: String
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        x = 1
        """,
    )

    assert test_project.generate() is True
    api = test_project.import_api()
    # attributes of a dynamically imported module are Any
    assert isinstance(api.API_CLIENT, runtime.GQLClient)  # pyright: ignore[reportAny]


def test_invalid_gql_call_arguments(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: String
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        q = api_gql(123)
        """,
    )

    with pytest.raises(TypeError, match="expected a single string literal"):
        test_project.generate()


def test_duplicate_identical_query_deduplication(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: String
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        first = api_gql("query Ping { ping }")
        second = api_gql("query Ping { ping }")
        """,
    )

    assert test_project.generate() is True


def test_duplicate_query_with_different_spelling_dispatches_both(
    test_project: ProjectBuilder,
):
    # Deduplication compares dedented text, so the same query indented
    # differently at two call sites is one operation — but the dispatch dict is
    # keyed by the exact literal, so every spelling must be present in it.
    test_project.prepare(
        schema="""
        type Query {
            ping: String
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        first = api_gql("query Ping { ping }")
        second = api_gql('''
            query Ping { ping }
        ''')
        """,
    )
    api_module, queries_module = test_project.generate_and_import()
    assert isinstance(queries_module.first, api_module.Ping)  # pyright: ignore[reportAny]
    assert isinstance(queries_module.second, api_module.Ping)  # pyright: ignore[reportAny]


def test_statically_empty_selection_is_rejected(test_project: ProjectBuilder):
    # A literal `@skip(if: true)` on every field leaves the model without
    # fields, and a fieldless class renders with an empty body that the
    # generated module cannot even import.
    test_project.prepare(
        schema="""
        type Query {
            user(id: ID!): User
        }

        type User {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    id @skip(if: true)
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(ValueError, match="statically empty"):
        test_project.generate()


def test_enum_sharing_a_model_raw_name_is_rejected(test_project: ProjectBuilder):
    # `q { child ... }` generates a model raw-named QResultChild; an enum with
    # the same schema name makes every NamedRef('QResultChild') ambiguous —
    # the subtree walks recurse through the model where the enum was meant.
    test_project.prepare(
        schema="""
        type Query {
            child: Child
        }

        type Child {
            id: ID!
            status: QResultChild
        }

        enum QResultChild {
            ACTIVE
            INACTIVE
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query q { child { id status } }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Enum 'QResultChild' shares"):
        test_project.generate()


def test_colliding_paths_with_different_shapes_are_rejected(
    test_project: ProjectBuilder,
):
    # `aB.c` and `a.bC` both concatenate to the raw name SResultABC; the
    # rename map is keyed by name, so it cannot give the two shapes distinct
    # detailed names — the collision is the developer's to resolve.
    test_project.prepare(
        schema="""
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
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query S { aB { c { id } } a { bC { id name } } }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match="colliding field paths with different shapes"
    ):
        test_project.generate()


def test_variable_mapping_to_python_keyword_is_rejected(test_project: ProjectBuilder):
    test_project.prepare(
        schema="""
        type Query {
            user(id: ID): User
        }

        type User {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($class: ID) { user(id: $class) { id } }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match=r"Execute parameter 'class'.*Python keyword"
    ):
        test_project.generate()


def test_field_aliased_to_python_keyword_is_rejected(test_project: ProjectBuilder):
    test_project.prepare(
        schema="""
        type Query {
            user: User
        }

        type User {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser { user { class: id } }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match=r"Field 'class'.*Python keyword"):
        test_project.generate()


def test_syntax_error_in_scanned_file(test_project: ProjectBuilder):
    test_project.prepare(
        schema="""
            type Query {
                ping: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        ping = api_gql(
            '''
            query Ping {
                ping
            }
            '''
        )
        """,
    )

    broken = test_project.root / "sample_app" / "broken.py"
    broken.write_text("api_gql(\ndef foo(\n", encoding="utf-8")

    with pytest.raises(SyntaxError, match=r"Failed to parse.*broken\.py"):
        test_project.generate()


def test_union_alias_colliding_with_a_model_name_is_rejected(
    test_project: ProjectBuilder,
):
    # `aB` (a union field) and `a.b` (an object path) both concatenate to the
    # raw name SResultAB — one a union alias, one a model; no rename can hold
    # both, so the collision is the developer's to resolve.
    test_project.prepare(
        schema="""
        type Query {
            aB: U
            a: Mid
        }

        union U = X | Y

        type X {
            id: ID!
        }

        type Y {
            name: String
        }

        type Mid {
            b: Z
        }

        type Z {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query S {
                aB { __typename ... on X { id } ... on Y { name } }
                a { b { id } }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="two colliding selections"):
        test_project.generate()


def test_two_response_keys_mapping_to_one_python_name_are_rejected(
    test_project: ProjectBuilder,
):
    # `userId` and `user_id` both snake to `user_id`: the class body would
    # declare the attribute twice and the second would silently win.
    test_project.prepare(
        schema="""
        type Query {
            user: User
        }

        type User {
            userId: ID!
            user_id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { user { userId user_id } }")
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="both map to Python name"):
        test_project.generate()


def test_field_shadowing_pydantic_protected_namespace_is_rejected(
    test_project: ProjectBuilder,
):
    # `modelDump` snakes to `model_dump`; pydantic strips such a field at
    # class creation, so the module would fail to import.
    test_project.prepare(
        schema="""
        type Query {
            image: Image
        }

        type Image {
            modelDump: String
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql("query Q { image { modelDump } }")
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="protected namespace"):
        test_project.generate()


async def test_defaulted_directive_variable_stays_conditional(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    # A default on the variable does not make @include static: the caller can
    # pass the other value at runtime, so the field is modeled optional and
    # both states validate real responses.
    async with test_project.server(
        httpserver,
        schema="""
        type Query {
            user: User
        }

        type User {
            id: ID!
            name: String
        }
        """,
        queries='''
        from sample_app.gql.api import api_gql

        q = api_gql(
            """
            query Q($withName: Boolean! = false) {
                user {
                    id
                    name @include(if: $withName)
                }
            }
            """
        )
        ''',
        resolvers={"Query": {"user": lambda *_: {"id": "u1", "name": "Alice"}}},
    ) as (_api_module, queries_module):
        on = await queries_module.q.execute(with_name=True)  # pyright: ignore[reportAny]
        assert on.user.name == "Alice"  # pyright: ignore[reportAny]
        off = await queries_module.q.execute(with_name=False)  # pyright: ignore[reportAny]
        assert off.user.name is None  # pyright: ignore[reportAny]
