from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from iron_gql.conftest import generate_api
from iron_gql.conftest import import_path
from iron_gql.conftest import prepare_workspace
from iron_gql.conftest import sample_app_context
from iron_gql.conftest import setup_test_server


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

    query_source = """
        from sample_app.gql.api import api_gql

        get_node_and_count = api_gql(
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
    """

    def resolve_node(_root, _info, *, id: str):
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Morty"}
        return {
            "__typename": "Admin",
            "id": id,
            "name": "Rick",
            "permissions": ["portal"],
        }

    def resolve_count(_root, _info):
        return 3

    with setup_test_server(
        tmp_path,
        httpserver,
        schema,
        query_source,
        {"Query": {"node": resolve_node, "count": resolve_count}},
    ) as (_, queries):
        result = await queries.get_node_and_count.execute(id="user-1")
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

    query_source = """
        from sample_app.gql.api import api_gql

        GET_ACTOR = api_gql(
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
    """

    def resolve_actor(_root, _info, *, id: str):
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Morty"}
        return {"__typename": "Admin", "id": id, "permissions": ["portal"]}

    with setup_test_server(
        tmp_path,
        httpserver,
        schema,
        query_source,
        {"Query": {"actor": resolve_actor}},
    ) as (api, queries):
        user_result = await queries.GET_ACTOR.execute(id="user-1")
        assert isinstance(user_result.actor, api.GetActorResultActorUser)
        assert user_result.actor.id == "user-1"
        assert user_result.actor.name == "Morty"

        admin_result = await queries.GET_ACTOR.execute(id="admin-1")
        assert isinstance(admin_result.actor, api.GetActorResultActorAdmin)
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

    query_source = """
        from sample_app.gql.api import api_gql

        GET_NODE = api_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    id
                }
            }
            '''
        )
    """

    def resolve_node(_root, _info, *, id: str):
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Morty"}
        return {"__typename": "Post", "id": id, "title": "GraphQL 101"}

    with setup_test_server(
        tmp_path,
        httpserver,
        schema,
        query_source,
        {"Query": {"node": resolve_node}},
    ) as (_, queries):
        result = await queries.GET_NODE.execute(id="user-1")
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

    query_source = """
        from sample_app.gql.api import api_gql

        GET_NODE = api_gql(
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
    """

    def resolve_node(_root, _info, *, id: str):
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Morty"}
        if id == "post-1":
            return {"__typename": "Post", "id": id, "title": "GraphQL 101"}
        return {"__typename": "Comment", "id": id, "body": "First!"}

    with setup_test_server(
        tmp_path,
        httpserver,
        schema,
        query_source,
        {"Query": {"node": resolve_node}},
    ) as (api, queries):
        user_result = await queries.GET_NODE.execute(id="user-1")
        assert isinstance(user_result.node, api.GetNodeResultNodeUser)
        assert user_result.node.name == "Morty"

        comment_result = await queries.GET_NODE.execute(id="comment-1")
        assert isinstance(comment_result.node, api.GetNodeResultNodeNode)
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

    query_source = """
        from sample_app.gql.api import api_gql

        GET_NODE = api_gql(
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
    """

    def resolve_node(_root, _info, *, id: str):
        return {
            "__typename": "User",
            "id": id,
            "child": {"__typename": "Comment", "id": "child-1"},
        }

    with setup_test_server(
        tmp_path,
        httpserver,
        schema,
        query_source,
        {"Query": {"node": resolve_node}},
    ) as (_, queries):
        result = await queries.GET_NODE.execute(id="user-1")
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

    query_source = """
        from sample_app.gql.api import api_gql

        GET_NODE = api_gql(
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
    """

    def resolve_node(_root, _info, *, id: str):
        if id == "user-1":
            return {
                "__typename": "User",
                "id": id,
                "createdAt": "2024-01-01",
                "name": "Morty",
            }
        return {"__typename": "Post", "id": id, "title": "GraphQL 101"}

    with setup_test_server(
        tmp_path,
        httpserver,
        schema,
        query_source,
        {"Query": {"node": resolve_node}},
    ) as (api, queries):
        user_result = await queries.GET_NODE.execute(id="user-1")
        assert isinstance(user_result.node, api.GetNodeResultNodeUser)
        assert user_result.node.created_at == "2024-01-01"

        post_result = await queries.GET_NODE.execute(id="post-1")
        assert isinstance(post_result.node, api.GetNodeResultNodeNode)
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
        from sample_app.gql.api import api_gql

        GET_NODE = api_gql(
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
        sample_app_context(tmp_path),
        pytest.raises(
            ValueError,
            match=r"Missing __typename in selection set for interface 'Node'",
        ),
    ):
        generate_api(tmp_path)


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
        from sample_app.gql.api import api_gql

        GET_NODE = api_gql(
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
        changed = generate_api(tmp_path)
        assert changed is False
        assert "Post" in caplog.text
        assert "Node" in caplog.text
