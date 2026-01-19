import importlib
from pathlib import Path

import graphql
import pytest
from pydantic import alias_generators

from iron_gql import runtime
from iron_gql.conftest import clear_sample_app_modules
from iron_gql.conftest import generate_api
from iron_gql.conftest import import_path
from iron_gql.conftest import prepare_workspace
from iron_gql.conftest import sample_app_context
from iron_gql.conftest import write_file
from iron_gql.generator import generate_gql_package


def test_generate_with_schema_outside_src(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    schema_path = tmp_path / "schema.graphql"
    write_file(
        schema_path,
        """
        type Query {
            ping: String!
        }
        """,
    )

    write_file(workspace / "sample_app/__init__.py", "")
    write_file(workspace / "sample_app/gql/__init__.py", "")
    write_file(
        workspace / "sample_app/settings.py",
        "GRAPHQL_URL = 'http://testserver/graphql/'\n",
    )
    write_file(
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

    with sample_app_context(workspace):
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

        module_path = workspace / "sample_app/gql/api.py"
        expected_schema_ref = schema_path.resolve().relative_to(
            workspace.resolve(), walk_up=True
        )
        generated = module_path.read_text(encoding="utf-8")
        assert f'Path("{expected_schema_ref}")' in generated

        clear_sample_app_modules()
        api_module = importlib.import_module("sample_app.gql.api")
        assert isinstance(api_module.API_CLIENT.schema, graphql.GraphQLSchema)


def test_duplicate_operations_raise(tmp_path: Path):
    schema = """
        type Query {
            user(id: ID!): User
        }

        type User {
            id: ID!
            name: String
        }
    """

    prepare_workspace(
        tmp_path,
        """
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
        schema=schema,
    )

    with (
        sample_app_context(tmp_path),
        pytest.raises(
            ValueError,
            match=r"^Cannot compile different GraphQL queries with same name",
        ),
    ):
        generate_api(tmp_path)


def test_nested_input_objects_missing(tmp_path: Path):
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

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.api import api_gql

        update_user = api_gql(
            '''
            mutation UpdateUser($input: UpdateUserInput!) {
                updateUser(input: $input)
            }
            '''
        )
        """,
        schema=schema,
    )

    with sample_app_context(tmp_path):
        changed = generate_api(tmp_path)
        assert changed is True

        clear_sample_app_modules()
        api_module = importlib.import_module("sample_app.gql.api")

        address = api_module.AddressInput(street="Main St")
        api_module.UpdateUserInput(id="u-1", address=address)


def test_invalid_query_reports_error(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    schema = """
        type Query {
            user: String
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.api import api_gql

        BROKEN = api_gql(
            '''
            query Broken {
                missingField
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path):
        caplog.set_level("ERROR")
        changed = generate_api(tmp_path)
        assert changed is False
        assert "missingField" in caplog.text


def test_fragment_cycle_reports_error(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    schema = """
        type Query {
            user: User
        }

        type User {
            id: ID!
            name: String!
        }
    """

    prepare_workspace(
        tmp_path,
        """
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
        schema=schema,
    )

    with import_path(tmp_path):
        caplog.set_level("ERROR")
        changed = generate_api(tmp_path)
        assert changed is False
        assert "Cannot spread fragment" in caplog.text
        assert not (tmp_path / "sample_app/gql/api.py").exists()


def test_input_type_dependency_ordering(tmp_path: Path):
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

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.api import api_gql

        create_order = api_gql(
            '''
            mutation CreateOrder($input: OrderInput!) {
                createOrder(input: $input)
            }
            '''
        )
        """,
        schema=schema,
    )

    with sample_app_context(tmp_path):
        changed = generate_api(tmp_path)
        assert changed is True

        generated = (tmp_path / "sample_app/gql/api.py").read_text()
        item_pos = generated.find("class ItemInput")
        order_pos = generated.find("class OrderInput")
        assert item_pos != -1, "ItemInput class not found"
        assert order_pos != -1, "OrderInput class not found"
        assert item_pos < order_pos, "ItemInput must appear before OrderInput"

        clear_sample_app_modules()
        api_module = importlib.import_module("sample_app.gql.api")
        item = api_module.ItemInput(sku="ABC123", quantity=2)
        api_module.OrderInput(id="o-1", item=item)


def test_self_referential_input_type(tmp_path: Path):
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

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.api import api_gql

        create_tree = api_gql(
            '''
            mutation CreateTree($root: TreeNode!) {
                createTree(root: $root)
            }
            '''
        )
        """,
        schema=schema,
    )

    with sample_app_context(tmp_path):
        changed = generate_api(tmp_path)
        assert changed is True

        generated = (tmp_path / "sample_app/gql/api.py").read_text()
        assert 'children: "list[TreeNode | None] | None"' in generated

        clear_sample_app_modules()
        api_module = importlib.import_module("sample_app.gql.api")
        leaf = api_module.TreeNode(value="leaf")
        parent = api_module.TreeNode(value="parent", children=[leaf])
        assert parent.children[0].value == "leaf"


def test_input_enums_and_defaults(tmp_path: Path):
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

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.api import api_gql

        update = api_gql(
            '''
            mutation Update($input: UpdateInput!) {
                update(input: $input)
            }
            '''
        )
        """,
        schema=schema,
    )

    with sample_app_context(tmp_path):
        changed = generate_api(tmp_path)
        assert changed is True

        generated = (tmp_path / "sample_app/gql/api.py").read_text()
        assert "type Status = Literal['ACTIVE', 'INACTIVE']" in generated
        assert "status: Status | None = None" in generated
        assert "note: str | None = None" in generated
        assert "child: \"ChildInput | None\" = {'code': 'X'}" in generated

        clear_sample_app_modules()
        api_module = importlib.import_module("sample_app.gql.api")
        update = api_module.UpdateInput(status="ACTIVE")
        assert isinstance(update.child, api_module.ChildInput)
        assert update.child.code == "X"
        serialized = runtime.serialize_var(update)
        assert serialized == {"status": "ACTIVE"}
