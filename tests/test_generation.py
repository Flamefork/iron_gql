import importlib

import pytest
from pydantic import alias_generators

from iron_gql import runtime
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


def test_invalid_query_reports_error(
    test_project: ProjectBuilder, caplog: pytest.LogCaptureFixture
):
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

    caplog.set_level("ERROR")
    changed = test_project.generate()
    assert changed is False
    assert "missingField" in caplog.text


def test_fragment_cycle_reports_error(
    test_project: ProjectBuilder, caplog: pytest.LogCaptureFixture
):
    schema = """
        type Query {
            user: User
        }

        type User {
            id: ID!
            name: String!
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        fragment_a = api_gql(
            '''
            fragment A on User {
                id
                ...B
            }
            '''
        )

        fragment_b = api_gql(
            '''
            fragment B on User {
                name
                ...A
            }
            '''
        )

        get_user = api_gql(
            '''
            query GetUser {
                user {
                    ...A
                }
            }
            '''
        )
        """,
    )

    caplog.set_level("ERROR")
    changed = test_project.generate()
    assert changed is False
    assert "Cannot spread fragment" in caplog.text
    assert not (test_project.root / "sample_app/gql/api.py").exists()


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

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class ItemInput" in generated
    assert "class OrderInput" in generated

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

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "children: list[TreeNode | None] | None = None" in generated

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

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "type Status = Literal['ACTIVE', 'INACTIVE']" in generated
    assert "status: Status | None = None" in generated
    assert "note: str | None = None" in generated
    assert "child: ChildInput | None = {'code': 'X'}" in generated

    api = test_project.import_api()
    update = api.UpdateInput(status="ACTIVE")
    assert isinstance(update.child, api.ChildInput)
    assert update.child.code == "X"
    serialized = runtime.serialize_var(update)
    assert serialized == {"status": "ACTIVE"}


def test_unknown_scalar_warning(
    test_project: ProjectBuilder, caplog: pytest.LogCaptureFixture
):
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

    caplog.set_level("WARNING")
    changed = test_project.generate()
    assert changed is True
    assert "Unknown scalar type CustomScalar" in caplog.text

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "custom_value: object | None" in generated


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

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "matrix: list[list[int | None] | None] | None = None" in generated


def test_anonymous_query_generation(test_project: ProjectBuilder):
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

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    # Verify class name starts with Query and looks like a hash fallback
    # The clean text of "query { ping }" should produce a consistent hash
    assert "class Query" in generated


def test_list_variable_argument(test_project: ProjectBuilder):
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

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    # Verify ListTypeNode parsing resulted in list typed argument
    assert "ids: list[" in generated
    assert "str | None" in generated
