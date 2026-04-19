import importlib

import pytest
from pydantic import alias_generators
from pytest_httpserver import HTTPServer

from iron_gql import runtime
from iron_gql.codegen import GraphQLGenerationError
from iron_gql.codegen import UnknownGQLTypeWarning
from iron_gql.codegen import generate_gql_package
from tests.conftest import ProjectBuilder


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
    assert isinstance(api_module.API_CLIENT, runtime.GQLClient)


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


def test_nested_input_objects_missing(test_project: ProjectBuilder):
    schema = """
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
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        update_user = api_gql(
            '''
            mutation UpdateUser($input: UpdateUserInput!) {
                updateUser(input: $input)
            }
            '''
        )
        """,
    )

    api, _ = test_project.generate_and_import()
    address = api.AddressInput(street="Main St")
    api.UpdateUserInput(id="u-1", address=address)


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
    item = api.ItemInput(sku="ABC123", quantity=2)
    api.OrderInput(id="o-1", item=item)


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
    leaf = api.TreeNode(value="leaf")
    parent = api.TreeNode(value="parent", children=[leaf])
    assert parent.children[0].value == "leaf"


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
    update = api.UpdateInput(status="ACTIVE")
    assert isinstance(update.child, api.ChildInput)
    assert update.child.code == "X"
    serialized = runtime.serialize_variables({"x": update})[0]["x"]
    assert serialized == {"status": "ACTIVE"}


async def test_operation_variable_enum_variable_executes(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            search(status: Status!): Boolean
        }

        enum Status {
            ACTIVE
            INACTIVE
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            query Search($status: Status!) {
                search(status: $status)
            }
            '''
        )
        """,
    )

    def resolve_search(_root, _info, *, status: str):
        return status == "ACTIVE"

    async with test_project.server(
        httpserver,
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            query Search($status: Status!) {
                search(status: $status)
            }
            '''
        )
        """,
        resolvers={"Query": {"search": resolve_search}},
    ) as (_, queries):
        active = await queries.search.execute(status="ACTIVE")
        assert active.search is True

        inactive = await queries.search.execute(status="INACTIVE")
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
    result = api.GetValueResult(custom_value={"raw": "value"})
    assert result.custom_value == {"raw": "value"}


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
        "input": api.ComplexInput(matrix=[[1, None], None])
    })
    assert variables == {"input": {"matrix": [[1, None], None]}}
    assert files == {}


async def test_anonymous_query_generation(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            ping: String
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        # Anonymous query
        q = api_gql("query { ping }")
        """,
    )

    def resolve_ping(_root, _info):
        return "pong"

    async with test_project.server(
        httpserver,
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql
        q = api_gql("query { ping }")
        """,
        resolvers={"Query": {"ping": resolve_ping}},
    ) as (_, queries):
        result = await queries.q.execute()
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

    def resolve_users(_root, _info, *, ids: list[str] | None = None):
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
        result = await queries.get_users.execute(ids=["u-1", "u-2"])
        assert [user.id for user in result.users] == ["u-1", "u-2"]


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
    assert isinstance(api.API_CLIENT, runtime.GQLClient)


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
