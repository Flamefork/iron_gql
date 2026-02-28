import importlib
import warnings

import pytest
from pydantic import alias_generators

from iron_gql import runtime
from iron_gql.codegen import GraphQLDeprecationWarning
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


def test_fragment_cycle_reports_error(test_project: ProjectBuilder):
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

    with pytest.raises(GraphQLGenerationError, match="Cannot spread fragment"):
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


def test_include_skip_directives(test_project: ProjectBuilder):
    schema = """
        type Query {
            user(id: ID!): User
        }
        type User {
            id: ID!
            name: String!
            email: String!
            phone: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            query GetUser($id: ID!, $withEmail: Boolean!, $skipPhone: Boolean!) {
                user(id: $id) {
                    name
                    email @include(if: $withEmail)
                    phone @skip(if: $skipPhone)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "name: str\n" in generated
    assert "email: str | None = None" in generated
    assert "phone: str | None = None" in generated


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


def test_one_of_basic_scalar_fields(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            search(criteria: SearchCriteria!): Boolean
        }

        input SearchCriteria @oneOf {
            name: String
            email: String
            userId: ID
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            mutation Search($criteria: SearchCriteria!) {
                search(criteria: $criteria)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class SearchCriteriaName(GQLModel):" in generated
    assert "class SearchCriteriaEmail(GQLModel):" in generated
    assert "class SearchCriteriaUserId(GQLModel):" in generated
    assert "type SearchCriteria = " in generated
    assert (
        "SearchCriteriaName | SearchCriteriaEmail | SearchCriteriaUserId" in generated
    )

    api = test_project.import_api()
    by_name = api.SearchCriteriaName(name="John")
    assert runtime.serialize_variables({"x": by_name})[0]["x"] == {"name": "John"}

    by_email = api.SearchCriteriaEmail(email="j@example.com")
    assert runtime.serialize_variables({"x": by_email})[0]["x"] == {
        "email": "j@example.com"
    }

    by_user_id = api.SearchCriteriaUserId(user_id="u-1")
    assert runtime.serialize_variables({"x": by_user_id})[0]["x"] == {"userId": "u-1"}


def test_one_of_with_nested_input_type(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            update(input: UpdateAction!): Boolean
        }

        input AddressInput {
            street: String!
            city: String!
        }

        input UpdateAction @oneOf {
            setName: String
            setAddress: AddressInput
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        update = api_gql(
            '''
            mutation Update($input: UpdateAction!) {
                update(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class UpdateActionSetName(GQLModel):" in generated
    assert "class UpdateActionSetAddress(GQLModel):" in generated
    assert (
        "type UpdateAction = UpdateActionSetName | UpdateActionSetAddress" in generated
    )

    api = test_project.import_api()
    addr = api.AddressInput(street="Main St", city="NYC")
    action = api.UpdateActionSetAddress(set_address=addr)
    assert runtime.serialize_variables({"x": action})[0]["x"] == {
        "setAddress": {"street": "Main St", "city": "NYC"}
    }


def test_one_of_referenced_in_regular_input(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            doSearch(input: WrapperInput!): Boolean
        }

        input SearchCriteria @oneOf {
            name: String
            email: String
        }

        input WrapperInput {
            criteria: SearchCriteria!
            limit: Int
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        do_search = api_gql(
            '''
            mutation DoSearch($input: WrapperInput!) {
                doSearch(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class WrapperInput(GQLModel):" in generated
    assert "criteria: SearchCriteria" in generated

    api = test_project.import_api()
    criteria = api.SearchCriteriaName(name="John")
    wrapper = api.WrapperInput(criteria=criteria, limit=10)
    assert runtime.serialize_variables({"x": wrapper})[0]["x"] == {
        "criteria": {"name": "John"},
        "limit": 10,
    }


def test_one_of_single_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            act(input: SingleChoice!): Boolean
        }

        input SingleChoice @oneOf {
            value: String
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        act = api_gql(
            '''
            mutation Act($input: SingleChoice!) {
                act(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class SingleChoiceValue(GQLModel):" in generated
    assert "type SingleChoice = SingleChoiceValue" in generated

    api = test_project.import_api()
    choice = api.SingleChoiceValue(value="hello")
    assert runtime.serialize_variables({"x": choice})[0]["x"] == {"value": "hello"}


def test_one_of_with_enum_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            filter(by: FilterBy!): Boolean
        }

        enum Status {
            ACTIVE
            INACTIVE
        }

        input FilterBy @oneOf {
            status: Status
            name: String
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        do_filter = api_gql(
            '''
            mutation Filter($by: FilterBy!) {
                filter(by: $by)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "Status = Literal['ACTIVE', 'INACTIVE']" in generated
    assert "class FilterByStatus(GQLModel):" in generated
    assert "class FilterByName(GQLModel):" in generated
    assert "type FilterBy = FilterByStatus | FilterByName" in generated

    api = test_project.import_api()
    by_status = api.FilterByStatus(status="ACTIVE")
    assert runtime.serialize_variables({"x": by_status})[0]["x"] == {"status": "ACTIVE"}

    by_name = api.FilterByName(name="Alice")
    assert runtime.serialize_variables({"x": by_name})[0]["x"] == {"name": "Alice"}


def test_one_of_with_list_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            search(by: SearchBy!): Boolean
        }

        input SearchBy @oneOf {
            name: String
            tags: [String!]
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            mutation Search($by: SearchBy!) {
                search(by: $by)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class SearchByName(GQLModel):" in generated
    assert "class SearchByTags(GQLModel):" in generated
    assert "type SearchBy = SearchByName | SearchByTags" in generated

    api = test_project.import_api()
    by_name = api.SearchByName(name="Alice")
    assert runtime.serialize_variables({"x": by_name})[0]["x"] == {"name": "Alice"}

    by_tags = api.SearchByTags(tags=["python", "graphql"])
    assert runtime.serialize_variables({"x": by_tags})[0]["x"] == {
        "tags": ["python", "graphql"]
    }


def test_one_of_referencing_one_of(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            act(input: OuterChoice!): Boolean
        }

        input InnerChoice @oneOf {
            x: String
            y: Int
        }

        input OuterChoice @oneOf {
            inner: InnerChoice
            direct: String
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        act = api_gql(
            '''
            mutation Act($input: OuterChoice!) {
                act(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "type InnerChoice = InnerChoiceX | InnerChoiceY" in generated
    assert "type OuterChoice = OuterChoiceInner | OuterChoiceDirect" in generated

    api = test_project.import_api()
    inner = api.InnerChoiceX(x="hello")
    outer = api.OuterChoiceInner(inner=inner)
    assert runtime.serialize_variables({"x": outer})[0]["x"] == {
        "inner": {"x": "hello"}
    }

    outer_direct = api.OuterChoiceDirect(direct="world")
    assert runtime.serialize_variables({"x": outer_direct})[0]["x"] == {
        "direct": "world"
    }


def test_deprecated_result_field_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }

        type User {
            id: ID!
            name: String @deprecated(reason: "Use fullName instead")
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            query GetUser {
                user {
                    id
                    name
                }
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=(
            r"Query 'GetUser': field 'User\.name'"
            r" is deprecated: Use fullName instead"
        ),
    ):
        test_project.generate()


def test_deprecated_argument_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            users(
                limit: Int
                first: Int @deprecated(reason: "Use limit instead")
            ): [String]
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_users = api_gql(
            '''
            query GetUsers($first: Int) {
                users(first: $first)
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=(
            r"Query 'GetUsers': argument 'first'"
            r" on 'Query\.users' is deprecated:"
            r" Use limit instead"
        ),
    ):
        test_project.generate()


def test_deprecated_input_field_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            update(input: UpdateInput!): Boolean
        }

        input UpdateInput {
            name: String!
            legacyName: String @deprecated(reason: "Use name instead")
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

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=r"Input field 'UpdateInput\.legacyName' is deprecated: Use name instead",
    ):
        test_project.generate()


def test_deprecated_enum_value_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            status: Status
        }

        enum Status {
            ACTIVE
            INACTIVE @deprecated(reason: "Use DISABLED instead")
            DISABLED
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_status = api_gql(
            '''
            query GetStatus {
                status
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=r"Enum value 'Status\.INACTIVE' is deprecated: Use DISABLED instead",
    ):
        test_project.generate()


def test_deprecated_one_of_input_field_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            search(criteria: SearchCriteria!): Boolean
        }

        input SearchCriteria @oneOf {
            name: String
            legacyId: ID @deprecated(reason: "Use name instead")
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            mutation Search($criteria: SearchCriteria!) {
                search(criteria: $criteria)
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=r"Input field 'SearchCriteria\.legacyId' is deprecated: Use name instead",
    ):
        test_project.generate()


def test_deprecated_field_without_reason(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }

        type User {
            id: ID!
            oldField: String @deprecated
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            query GetUser {
                user {
                    id
                    oldField
                }
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=(
            r"Query 'GetUser': field 'User\.oldField'"
            r" is deprecated: No longer supported"
        ),
    ):
        test_project.generate()


def test_deprecated_field_in_union(test_project: ProjectBuilder):
    schema = """
        type Query {
            search: SearchResult
        }

        union SearchResult = User | Post

        type User {
            id: ID!
            legacyName: String @deprecated(reason: "Use name instead")
        }

        type Post {
            id: ID!
            title: String
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            query Search {
                search {
                    __typename
                    ... on User {
                        id
                        legacyName
                    }
                    ... on Post {
                        id
                        title
                    }
                }
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=(
            r"Query 'Search': field 'User\.legacyName'"
            r" is deprecated: Use name instead"
        ),
    ):
        test_project.generate()


def test_deprecated_argument_not_used_no_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            users(
                limit: Int
                first: Int @deprecated(reason: "Use limit instead")
            ): [String]
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_users = api_gql(
            '''
            query GetUsers($limit: Int) {
                users(limit: $limit)
            }
            '''
        )
        """,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", GraphQLDeprecationWarning)
        test_project.generate()


def test_no_deprecated_no_warnings(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        get_user = api_gql(
            '''
            query GetUser {
                user {
                    id
                    name
                }
            }
            '''
        )
        """,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", GraphQLDeprecationWarning)
        test_project.generate()


def test_include_on_non_null_field(test_project: ProjectBuilder):
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

        q = api_gql(
            '''
            query GetUser($withName: Boolean!) {
                user {
                    id
                    name @include(if: $withName)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str | None = None" in generated


def test_skip_on_non_null_field(test_project: ProjectBuilder):
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

        q = api_gql(
            '''
            query GetUser($skipName: Boolean!) {
                user {
                    id
                    name @skip(if: $skipName)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str | None = None" in generated


def test_include_on_nullable_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        q = api_gql(
            '''
            query GetUser($withName: Boolean!) {
                user {
                    id
                    name @include(if: $withName)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str | None = None" in generated


def test_include_on_inline_fragment(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String!
            email: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($withDetails: Boolean!) {
                user {
                    id
                    ... @include(if: $withDetails) {
                        name
                        email
                    }
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str | None = None" in generated
    assert "email: str | None = None" in generated


def test_field_both_conditional_and_unconditional(test_project: ProjectBuilder):
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

        q = api_gql(
            '''
            query GetUser($withDetails: Boolean!) {
                user {
                    id
                    name
                    ... @include(if: $withDetails) {
                        name
                    }
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str\n" in generated


def test_skip_with_literal_false(test_project: ProjectBuilder):
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

        q = api_gql(
            '''
            query GetUser {
                user {
                    id
                    name @skip(if: false)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str\n" in generated


def test_include_and_skip_on_same_field(test_project: ProjectBuilder):
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

        q = api_gql(
            '''
            query GetUser($show: Boolean!, $hide: Boolean!) {
                user {
                    id
                    name @include(if: $show) @skip(if: $hide)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "name: str | None = None" in generated


def test_include_on_camel_case_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            firstName: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($withName: Boolean!) {
                user {
                    id
                    firstName @include(if: $withName)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "first_name: str | None = None" in generated


def test_include_on_non_null_list_of_nullable(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            tags: [String]!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($withTags: Boolean!) {
                user {
                    id
                    tags @include(if: $withTags)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "tags: list[str | None] | None = None" in generated


def test_include_with_literal_true(test_project: ProjectBuilder):
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

        q = api_gql(
            '''
            query GetUser {
                user {
                    id
                    name @include(if: true)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str\n" in generated


def test_include_on_nested_object_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            address: Address!
        }
        type Address {
            city: String!
            zip: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($withAddress: Boolean!) {
                user {
                    id
                    address @include(if: $withAddress) {
                        city
                        zip
                    }
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "address: GetUserResultUserAddress | None = None" in generated
    assert "class GetUserResultUserAddress(GQLModel):" in generated
    assert "city: str\n" in generated
    assert "zip: str\n" in generated


def test_shared_variable_in_include_and_skip(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            email: String!
            phone: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($flag: Boolean!) {
                user {
                    id
                    email @include(if: $flag)
                    phone @skip(if: $flag)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "email: str | None = None" in generated
    assert "phone: str | None = None" in generated


def test_include_skip_inside_named_fragment(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String!
            email: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($withEmail: Boolean!) {
                user {
                    id
                    ...UserDetails
                }
            }

            fragment UserDetails on User {
                name
                email @include(if: $withEmail)
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str\n" in generated
    assert "email: str | None = None" in generated


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

    events_type = api.EventsResultEvents
    assert "id" in events_type.model_fields
    assert "message" in events_type.model_fields

    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class Events(runtime.GQLOperation):" in generated
    assert "async def execute(" in generated
    assert "AsyncGenerator[EventsResult]" in generated
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
