import importlib

import pytest
from pydantic import alias_generators

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
    serialized = runtime.serialize_variables({"x": update})[0]["x"]
    assert serialized == {"status": "ACTIVE"}


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
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "GQLClient" in generated
    dispatch_decl = "_API_GQL_DISPATCH: dict[str, type[runtime.GQLOperation]] = {\n\n}"
    assert dispatch_decl in generated


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


def test_subscription_generates_subscription_class(test_project: ProjectBuilder):
    test_project.prepare(
        schema="""
            type Query {
                _dummy: String
            }

            type Subscription {
                events(channel: String!): Event!
            }

            type Event {
                id: ID!
                message: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        events = api_gql(
            '''
            subscription Events($channel: String!) {
                events(channel: $channel) {
                    id
                    message
                }
            }
            '''
        )
        """,
    )

    api, queries = test_project.generate_and_import()

    assert issubclass(api.Events, runtime.GQLOperation)
    assert hasattr(queries.events, "execute")
    assert not hasattr(queries.events, "subscribe")

    assert hasattr(api, "EventsResult")
    result_fields = api.EventsResult.model_fields
    assert "events" in result_fields

    events_type = api.EventWithIdMessage
    assert "id" in events_type.model_fields
    assert "message" in events_type.model_fields

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class Events(runtime.GQLOperation):" in generated
    assert "def execute(" in generated
    assert "AbstractAsyncContextManager[AsyncGenerator[EventsResult]]" in generated
    assert "API_CLIENT.subscribe(" in generated


def test_subscription_dispatch_fn(test_project: ProjectBuilder):
    test_project.prepare(
        schema="""
            type Query {
                ping: String!
            }

            type Subscription {
                events: String!
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

        events = api_gql(
            '''
            subscription Events {
                events
            }
            '''
        )
        """,
    )

    _api, queries = test_project.generate_and_import()

    assert isinstance(queries.ping, runtime.GQLOperation)
    assert isinstance(queries.events, runtime.GQLOperation)


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


def test_sort_by_source_location(test_project: ProjectBuilder):
    test_project.prepare(
        schema="""
            type Query {
                ping: String!
                pong: String!
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        pong = api_gql(
            '''
            query Pong {
                pong
            }
            '''
        )
        """,
    )

    test_project.write_file(
        test_project.root / "sample_app" / "zzz.py",
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

    _api, _ = test_project.generate_and_import()

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    pong_pos = generated.index("class Pong(")
    ping_pos = generated.index("class Ping(")
    assert pong_pos < ping_pos


def test_source_location_comments(test_project: ProjectBuilder):
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

    _api, _ = test_project.generate_and_import()

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "# See: sample_app/queries.py:3" in generated


def test_source_location_comments_deduplicated(test_project: ProjectBuilder):
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

    test_project.write_file(
        test_project.root / "sample_app" / "other.py",
        """
        from sample_app.gql.api import api_gql

        ping2 = api_gql(
            '''
            query Ping {
                ping
            }
            '''
        )
        """,
    )

    _api, _ = test_project.generate_and_import()

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "# See: " in generated
    see_line = next(line for line in generated.splitlines() if "# See:" in line)
    assert "sample_app/queries.py:3" in see_line
    assert "sample_app/other.py:3" in see_line
