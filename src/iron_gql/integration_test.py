import importlib
import json
import os
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path

import graphql
import pytest
from pydantic import ValidationError
from pydantic import alias_generators
from pytest_httpserver import HTTPServer
from pytest_httpserver.httpserver import HandlerType
from werkzeug import Response

from iron_gql.generator import generate_gql_package


def prepare_workspace(tmp_path: Path, query_source: str, schema: str):
    def write(path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")

    write(tmp_path / "schema.graphql", schema)
    write(tmp_path / "sample_app/__init__.py", "")
    write(tmp_path / "sample_app/gql/__init__.py", "")
    write(
        tmp_path / "sample_app/settings.py",
        "GRAPHQL_URL = 'http://testserver/graphql/'\n",
    )
    write(tmp_path / "sample_app/queries.py", query_source)


def clear_sample_app_modules():
    for module_name in list(sys.modules):
        if module_name == "sample_app" or module_name.startswith("sample_app."):
            sys.modules.pop(module_name, None)


@contextmanager
def import_path(path: Path):
    sys.path.insert(0, str(path))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        sys.path.remove(str(path))
        importlib.invalidate_caches()


@contextmanager
def working_directory(path: Path):
    current = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(current)


def generate_gateway(tmp_path: Path):
    return generate_gql_package(
        schema_path=tmp_path / "schema.graphql",
        package_full_name="sample_app.gql.gateway",
        base_url_import="sample_app.settings:GRAPHQL_URL",
        scalars={"ID": "builtins:str"},
        to_camel_fn_full_name="pydantic.alias_generators:to_camel",
        to_snake_fn=alias_generators.to_snake,
        src_path=tmp_path,
    )


def load_sample_app_modules():
    clear_sample_app_modules()
    gateway_module = importlib.import_module("sample_app.gql.gateway")
    queries_module = importlib.import_module("sample_app.queries")
    return gateway_module, queries_module


def build_schema_with_resolver(schema: str, field_name: str, resolver):
    schema_obj = graphql.build_schema(schema)
    query_type = schema_obj.get_type("Query")
    assert isinstance(query_type, graphql.GraphQLObjectType)
    query_type.fields[field_name].resolve = resolver
    return schema_obj


def setup_httpserver(httpserver: HTTPServer, schema_obj: graphql.GraphQLSchema) -> str:
    def graphql_handler(request):
        payload = request.get_json(silent=True) or {}
        result = graphql.graphql_sync(
            schema_obj,
            payload.get("query", ""),
            variable_values=payload.get("variables"),
            operation_name=payload.get("operationName"),
        )
        return Response(
            json.dumps(result.formatted),
            status=200,
            mimetype="application/json",
        )

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        graphql_handler
    )
    return httpserver.url_for("/graphql/")


async def test_generate_and_execute_queries(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        type Query {
            user(id: ID!): User
        }

        type Mutation {
            updateUser(input: UpdateUserInput!): User
        }

        type User {
            id: ID!
            name: String!
        }

        input UpdateUserInput {
            id: ID!
            name: String!
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        get_user = gateway_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    id
                    name
                }
            }
            '''
        )

        update_user = gateway_gql(
            '''
            mutation UpdateUser($input: UpdateUserInput!) {
                updateUser(input: $input) {
                    id
                    name
                }
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        state = {"user-1": "Graph"}

        schema_obj = graphql.build_schema(schema)

        query_type = schema_obj.get_type("Query")
        mutation_type = schema_obj.get_type("Mutation")
        assert isinstance(query_type, graphql.GraphQLObjectType)
        assert isinstance(mutation_type, graphql.GraphQLObjectType)

        def resolve_user(_root, _info, *, id: str):
            name = state.get(id)
            if name is None:
                return None
            return {"id": id, "name": name}

        def resolve_update_user(_root, _info, **kwargs):
            input_data = kwargs["input"]
            user_id = str(input_data["id"])
            state[user_id] = input_data["name"]
            return {"id": user_id, "name": input_data["name"]}

        query_type.fields["user"].resolve = resolve_user
        mutation_type.fields["updateUser"].resolve = resolve_update_user

        gateway_module.GATEWAY_CLIENT.base_url = setup_httpserver(
            httpserver, schema_obj
        )

        read_query = queries_module.get_user.with_headers({
            "Authorization": "Bearer token"
        })
        initial = await read_query.execute(id="user-1")
        assert initial.user is not None
        assert initial.user.name == "Graph"

        mutation_input = gateway_module.UpdateUserInput(id="user-1", name="Morty")
        updated = await queries_module.update_user.execute(input=mutation_input)
        assert updated.update_user.name == "Morty"
        refreshed = await queries_module.get_user.execute(id="user-1")
        assert refreshed.user is not None
        assert refreshed.user.name == "Morty"


def test_generate_with_schema_outside_src(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    schema_path = tmp_path / "schema.graphql"
    schema_path.write_text(
        textwrap.dedent(
            """
            type Query {
                ping: String!
            }
            """
        ).lstrip("\n"),
        encoding="utf-8",
    )

    (workspace / "sample_app").mkdir(parents=True, exist_ok=True)
    (workspace / "sample_app/gql").mkdir(parents=True, exist_ok=True)
    (workspace / "sample_app/__init__.py").write_text("", encoding="utf-8")
    (workspace / "sample_app/gql/__init__.py").write_text("", encoding="utf-8")
    (workspace / "sample_app/settings.py").write_text(
        "GRAPHQL_URL = 'http://testserver/graphql/'\n",
        encoding="utf-8",
    )
    (workspace / "sample_app/queries.py").write_text(
        textwrap.dedent(
            """
            from sample_app.gql.gateway import gateway_gql

            ping = gateway_gql(
                '''
                query Ping {
                    ping
                }
                '''
            )
            """
        ).lstrip("\n"),
        encoding="utf-8",
    )

    with import_path(workspace), working_directory(workspace):
        changed = generate_gql_package(
            schema_path=schema_path,
            package_full_name="sample_app.gql.gateway",
            base_url_import="sample_app.settings:GRAPHQL_URL",
            scalars={"ID": "builtins:str"},
            to_camel_fn_full_name="pydantic.alias_generators:to_camel",
            to_snake_fn=alias_generators.to_snake,
            src_path=workspace,
        )
        assert changed is True

        module_path = workspace / "sample_app/gql/gateway.py"
        expected_schema_ref = schema_path.resolve().relative_to(
            workspace.resolve(), walk_up=True
        )
        generated = module_path.read_text(encoding="utf-8")
        assert f'Path("{expected_schema_ref}")' in generated

        clear_sample_app_modules()
        gateway_module = importlib.import_module("sample_app.gql.gateway")
        assert isinstance(gateway_module.GATEWAY_CLIENT.schema, graphql.GraphQLSchema)


async def test_union_result_validation(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        type Query {
            node(id: ID!): Node
            count: Int!
        }

        union Node = User | Admin

        type User {
            id: ID!
            name: String!
        }

        type Admin {
            id: ID!
            name: String!
            permissions: [String!]!
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        get_node_and_count = gateway_gql(
            '''
            query GetNodeAndCount($id: ID!) {
                node(id: $id) {
                    __typename
                    ... on User {
                        id
                        name
                    }
                    ... on Admin {
                        id
                        name
                        permissions
                    }
                }
                count
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        schema_obj = graphql.build_schema(schema)
        query_type = schema_obj.get_type("Query")
        assert isinstance(query_type, graphql.GraphQLObjectType)

        def resolve_node(_root, _info, *, id: str):
            if id == "user-1":
                return {
                    "__typename": "User",
                    "id": id,
                    "name": "Morty",
                }
            return {
                "__typename": "Admin",
                "id": id,
                "name": "Rick",
                "permissions": ["portal"],
            }

        def resolve_count(_root, _info):
            return 3

        query_type.fields["node"].resolve = resolve_node
        query_type.fields["count"].resolve = resolve_count

        gateway_module.GATEWAY_CLIENT.base_url = setup_httpserver(
            httpserver, schema_obj
        )

        result = await queries_module.get_node_and_count.execute(id="user-1")
        assert result.node is not None
        assert result.count == 3


async def test_union_with_interface_fragment(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String!
        }

        type Admin implements Node {
            id: ID!
            permissions: [String!]!
        }

        union Actor = User | Admin

        type Query {
            actor(id: ID!): Actor
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        GET_ACTOR = gateway_gql(
            '''
            query GetActor($id: ID!) {
                actor(id: $id) {
                    __typename
                    ... on Node {
                        id
                    }
                    ... on User {
                        name
                    }
                    ... on Admin {
                        permissions
                    }
                }
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        def resolve_actor(_root, _info, *, id: str):
            if id == "user-1":
                return {
                    "__typename": "User",
                    "id": id,
                    "name": "Morty",
                }
            return {
                "__typename": "Admin",
                "id": id,
                "permissions": ["portal"],
            }

        schema_obj = build_schema_with_resolver(schema, "actor", resolve_actor)
        gateway_module.GATEWAY_CLIENT.base_url = setup_httpserver(
            httpserver, schema_obj
        )

        user_result = await queries_module.GET_ACTOR.execute(id="user-1")
        assert isinstance(user_result.actor, gateway_module.GetActorResultActorUser)
        assert user_result.actor.id == "user-1"
        assert user_result.actor.name == "Morty"

        admin_result = await queries_module.GET_ACTOR.execute(id="admin-1")
        assert isinstance(admin_result.actor, gateway_module.GetActorResultActorAdmin)
        assert admin_result.actor.id == "admin-1"
        assert admin_result.actor.permissions == ["portal"]


async def test_interface_without_fragments(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String
        }

        type Post implements Node {
            id: ID!
            title: String
        }

        type Query {
            node(id: ID!): Node
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        GET_NODE = gateway_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    id
                }
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        def resolve_node(_root, _info, *, id: str):
            if id == "user-1":
                return {"__typename": "User", "id": id, "name": "Morty"}
            return {"__typename": "Post", "id": id, "title": "GraphQL 101"}

        schema_obj = build_schema_with_resolver(schema, "node", resolve_node)
        gateway_module.GATEWAY_CLIENT.base_url = setup_httpserver(
            httpserver, schema_obj
        )

        result = await queries_module.GET_NODE.execute(id="user-1")
        assert result.node is not None
        assert result.node.id == "user-1"


async def test_interface_with_fragments(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String
        }

        type Post implements Node {
            id: ID!
            title: String
        }

        type Comment implements Node {
            id: ID!
            body: String
        }

        type Query {
            node(id: ID!): Node
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        GET_NODE = gateway_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    __typename
                    id
                    ... on User {
                        name
                    }
                    ... on Post {
                        title
                    }
                }
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        def resolve_node(_root, _info, *, id: str):
            if id == "user-1":
                return {
                    "__typename": "User",
                    "id": id,
                    "name": "Morty",
                }
            if id == "post-1":
                return {
                    "__typename": "Post",
                    "id": id,
                    "title": "GraphQL 101",
                }
            return {
                "__typename": "Comment",
                "id": id,
                "body": "First!",
            }

        schema_obj = build_schema_with_resolver(schema, "node", resolve_node)
        gateway_module.GATEWAY_CLIENT.base_url = setup_httpserver(
            httpserver, schema_obj
        )

        user_result = await queries_module.GET_NODE.execute(id="user-1")
        assert isinstance(user_result.node, gateway_module.GetNodeResultNodeUser)
        assert user_result.node.name == "Morty"

        comment_result = await queries_module.GET_NODE.execute(id="comment-1")
        assert isinstance(comment_result.node, gateway_module.GetNodeResultNodeNode)
        assert comment_result.node.id == "comment-1"


async def test_nested_interface(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        interface Child {
            id: ID!
        }

        interface Node {
            id: ID!
            child: Child
        }

        type User implements Node {
            id: ID!
            child: Child
        }

        type Post implements Node {
            id: ID!
            child: Child
        }

        type Comment implements Child {
            id: ID!
        }

        type Query {
            node(id: ID!): Node
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        GET_NODE = gateway_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    __typename
                    id
                    child {
                        id
                    }
                }
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        def resolve_node(_root, _info, *, id: str):
            return {
                "__typename": "User",
                "id": id,
                "child": {"__typename": "Comment", "id": "child-1"},
            }

        schema_obj = build_schema_with_resolver(schema, "node", resolve_node)
        gateway_module.GATEWAY_CLIENT.base_url = setup_httpserver(
            httpserver, schema_obj
        )

        result = await queries_module.GET_NODE.execute(id="user-1")
        assert result.node is not None
        assert result.node.child is not None
        assert result.node.child.id == "child-1"


async def test_interface_hierarchy(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        interface Node {
            id: ID!
        }

        interface Entity implements Node {
            id: ID!
            createdAt: String!
        }

        type User implements Entity & Node {
            id: ID!
            createdAt: String!
            name: String
        }

        type Post implements Node {
            id: ID!
            title: String
        }

        type Query {
            node(id: ID!): Node
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        GET_NODE = gateway_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    __typename
                    id
                    ... on Entity {
                        createdAt
                    }
                }
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        def resolve_node(_root, _info, *, id: str):
            if id == "user-1":
                return {
                    "__typename": "User",
                    "id": id,
                    "createdAt": "2024-01-01",
                    "name": "Morty",
                }
            return {
                "__typename": "Post",
                "id": id,
                "title": "GraphQL 101",
            }

        schema_obj = build_schema_with_resolver(schema, "node", resolve_node)
        gateway_module.GATEWAY_CLIENT.base_url = setup_httpserver(
            httpserver, schema_obj
        )

        user_result = await queries_module.GET_NODE.execute(id="user-1")
        assert isinstance(user_result.node, gateway_module.GetNodeResultNodeUser)
        assert user_result.node.created_at == "2024-01-01"

        post_result = await queries_module.GET_NODE.execute(id="post-1")
        assert isinstance(post_result.node, gateway_module.GetNodeResultNodeNode)
        assert post_result.node.id == "post-1"


def test_interface_fragment_requires_typename(tmp_path: Path):
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String
        }

        type Query {
            node(id: ID!): Node
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        GET_NODE = gateway_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    id
                    ... on User {
                        name
                    }
                }
            }
            '''
        )
        """,
        schema=schema,
    )

    with (
        import_path(tmp_path),
        working_directory(tmp_path),
        pytest.raises(
            ValueError,
            match=r"Missing __typename in selection set for interface 'Node'",
        ),
    ):
        generate_gateway(tmp_path)


def test_invalid_interface_fragment_reports_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String
        }

        type Post {
            id: ID!
            title: String
        }

        type Query {
            node(id: ID!): Node
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        GET_NODE = gateway_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    __typename
                    id
                    ... on Post {
                        title
                    }
                }
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path):
        caplog.set_level("ERROR")
        changed = generate_gateway(tmp_path)
        assert changed is False
        assert "Post" in caplog.text
        assert "Node" in caplog.text


async def test_list_allows_null_elements(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        type Query {
            numbers1: [Int]!
            numbers2: [Int!]
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        NUMBERS = gateway_gql(
            '''
            query Numbers {
                numbers1
                numbers2
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        httpserver.expect_request(
            "/graphql/",
            method="POST",
            handler_type=HandlerType.ONESHOT,
        ).respond_with_json({"data": {"numbers1": [1, None], "numbers2": [1, 2]}})

        gateway_module.GATEWAY_CLIENT.base_url = httpserver.url_for("/graphql/")

        response = await queries_module.NUMBERS.execute()
        assert response.numbers_1 == [1, None]
        assert response.numbers_2 == [1, 2]

        httpserver.expect_request(
            "/graphql/",
            method="POST",
            handler_type=HandlerType.ONESHOT,
        ).respond_with_json({"data": {"numbers1": [1, 2], "numbers2": [1, None]}})

        with pytest.raises(ValidationError):
            await queries_module.NUMBERS.execute()


async def test_variable_defaults_optional(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        type Query {
            posts(limit: Int = 5): [Int!]!
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        GET_POSTS = gateway_gql(
            '''
            query GetPosts($limit: Int = 5) {
                posts(limit: $limit)
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        schema_obj = graphql.build_schema(schema)
        query_type = schema_obj.get_type("Query")
        assert isinstance(query_type, graphql.GraphQLObjectType)

        def resolve_posts(_root, _info, *, limit: int = 5):
            return list(range(limit))

        query_type.fields["posts"].resolve = resolve_posts

        gateway_module.GATEWAY_CLIENT.base_url = setup_httpserver(
            httpserver, schema_obj
        )

        default_result = await queries_module.GET_POSTS.execute()
        assert default_result.posts == [0, 1, 2, 3, 4]

        explicit_result = await queries_module.GET_POSTS.execute(limit=2)
        assert explicit_result.posts == [0, 1]


async def test_inline_fragment_without_type_condition(
    tmp_path: Path, httpserver: HTTPServer
):
    schema = """
        type Query {
            viewer: User!
        }

        type User {
            id: ID!
            name: String!
            email: String!
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        GET_VIEWER = gateway_gql(
            '''
            query GetViewer {
                viewer {
                    id
                    ... {
                        name
                        email
                    }
                }
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        gateway_module, queries_module = load_sample_app_modules()

        schema_obj = graphql.build_schema(schema)
        query_type = schema_obj.get_type("Query")
        assert isinstance(query_type, graphql.GraphQLObjectType)

        def resolve_viewer(_root, _info):
            return {"id": "user-1", "name": "Morty", "email": "morty@example.com"}

        query_type.fields["viewer"].resolve = resolve_viewer

        gateway_module.GATEWAY_CLIENT.base_url = setup_httpserver(
            httpserver, schema_obj
        )

        result = await queries_module.GET_VIEWER.execute()
        assert result.viewer.id == "user-1"
        assert result.viewer.name == "Morty"
        assert result.viewer.email == "morty@example.com"


def test_duplicate_operations_raise(tmp_path: Path):
    schema = """
        type Query {
            user(id: ID!): User
        }

        type User {
            id: ID!
            name: String!
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        first_query = gateway_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    id
                }
            }
            '''
        )

        second_query = gateway_gql(
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
        import_path(tmp_path),
        working_directory(tmp_path),
        pytest.raises(
            ValueError,
            match=r"^Cannot compile different GraphQL queries with same name",
        ),
    ):
        generate_gateway(tmp_path)


def test_nested_input_objects_missing(tmp_path: Path):
    schema = """
        type Query {
            ping: Boolean!
        }

        type Mutation {
            updateUser(input: UpdateUserInput!): Boolean!
        }

        input UpdateUserInput {
            id: ID!
            address: AddressInput!
        }

        input AddressInput {
            street: String!
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        update_user = gateway_gql(
            '''
            mutation UpdateUser($input: UpdateUserInput!) {
                updateUser(input: $input)
            }
            '''
        )
        """,
        schema=schema,
    )

    with import_path(tmp_path), working_directory(tmp_path):
        changed = generate_gateway(tmp_path)
        assert changed is True

        clear_sample_app_modules()
        gateway_module = importlib.import_module("sample_app.gql.gateway")

        address = gateway_module.AddressInput(street="Main St")
        gateway_module.UpdateUserInput(id="u-1", address=address)


def test_invalid_query_reports_error(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    schema = """
        type Query {
            userName(id: ID!): String
        }
    """

    prepare_workspace(
        tmp_path,
        """
        from sample_app.gql.gateway import gateway_gql

        BROKEN = gateway_gql(
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
        changed = generate_gateway(tmp_path)
        assert changed is False
        assert "missingField" in caplog.text
        assert not (tmp_path / "sample_app/gql/gateway.py").exists()
