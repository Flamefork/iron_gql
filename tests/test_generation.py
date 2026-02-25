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
    serialized = runtime.serialize_var(update)
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
    assert "name: str" in generated
    assert "email: str" in generated
    assert "phone: str" in generated


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
    assert "_API_GQL_DISPATCH: dict[str, type[runtime.GQLQuery]] = {\n\n}" in generated


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
    assert runtime.serialize_var(by_name) == {"name": "John"}

    by_email = api.SearchCriteriaEmail(email="j@example.com")
    assert runtime.serialize_var(by_email) == {"email": "j@example.com"}

    by_user_id = api.SearchCriteriaUserId(user_id="u-1")
    assert runtime.serialize_var(by_user_id) == {"userId": "u-1"}


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
    assert runtime.serialize_var(action) == {
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
    assert runtime.serialize_var(wrapper) == {
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
    assert runtime.serialize_var(choice) == {"value": "hello"}


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
    assert runtime.serialize_var(by_status) == {"status": "ACTIVE"}

    by_name = api.FilterByName(name="Alice")
    assert runtime.serialize_var(by_name) == {"name": "Alice"}


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
    assert runtime.serialize_var(by_name) == {"name": "Alice"}

    by_tags = api.SearchByTags(tags=["python", "graphql"])
    assert runtime.serialize_var(by_tags) == {"tags": ["python", "graphql"]}


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
    assert runtime.serialize_var(outer) == {"inner": {"x": "hello"}}

    outer_direct = api.OuterChoiceDirect(direct="world")
    assert runtime.serialize_var(outer_direct) == {"direct": "world"}
