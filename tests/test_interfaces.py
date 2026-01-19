import pytest
from pytest_httpserver import HTTPServer

from tests.conftest import ProjectBuilder


async def test_union_result_validation(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
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

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"node": resolve_node, "count": resolve_count}},
    ) as (_, queries):
        result = await queries.get_node_and_count.execute(id="user-1")
        assert result.node is not None
        assert result.count == 3


async def test_union_with_interface_fragment(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
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

        get_actor = api_gql(
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

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"actor": resolve_actor}},
    ) as (api, queries):
        user_result = await queries.get_actor.execute(id="user-1")
        assert isinstance(user_result.actor, api.GetActorResultActorUser)
        assert user_result.actor.id == "user-1"
        assert user_result.actor.name == "Morty"

        admin_result = await queries.get_actor.execute(id="admin-1")
        assert isinstance(admin_result.actor, api.GetActorResultActorAdmin)
        assert admin_result.actor.id == "admin-1"
        assert admin_result.actor.permissions == ["portal"]


async def test_interface_without_fragments(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
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

        get_node = api_gql(
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

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"node": resolve_node}},
    ) as (_, queries):
        result = await queries.get_node.execute(id="user-1")
        assert result.node is not None
        assert result.node.id == "user-1"


async def test_interface_with_fragments(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
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

        get_node = api_gql(
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

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"node": resolve_node}},
    ) as (api, queries):
        user_result = await queries.get_node.execute(id="user-1")
        assert isinstance(user_result.node, api.GetNodeResultNodeUser)
        assert user_result.node.name == "Morty"

        comment_result = await queries.get_node.execute(id="comment-1")
        assert isinstance(comment_result.node, api.GetNodeResultNodeNode)
        assert comment_result.node.id == "comment-1"


async def test_nested_interface(test_project: ProjectBuilder, httpserver: HTTPServer):
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

        get_node = api_gql(
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

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"node": resolve_node}},
    ) as (_, queries):
        result = await queries.get_node.execute(id="user-1")
        assert result.node is not None
        assert result.node.child is not None
        assert result.node.child.id == "child-1"


async def test_interface_hierarchy(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
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

        get_node = api_gql(
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

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"node": resolve_node}},
    ) as (api, queries):
        user_result = await queries.get_node.execute(id="user-1")
        assert isinstance(user_result.node, api.GetNodeResultNodeUser)
        assert user_result.node.created_at == "2024-01-01"

        post_result = await queries.get_node.execute(id="post-1")
        assert isinstance(post_result.node, api.GetNodeResultNodeNode)
        assert post_result.node.id == "post-1"


def test_interface_fragment_requires_typename(test_project: ProjectBuilder):
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

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
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
    )

    with pytest.raises(
        ValueError,
        match=r"Missing __typename in selection set for interface 'Node'",
    ):
        test_project.generate()


def test_invalid_interface_fragment_reports_error(
    test_project: ProjectBuilder, caplog: pytest.LogCaptureFixture
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

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
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
    )

    caplog.set_level("ERROR")
    changed = test_project.generate()
    assert changed is False
    assert "Post" in caplog.text
    assert "Node" in caplog.text


def test_union_fragment_requires_typename(test_project: ProjectBuilder):
    schema = """
        union SearchResult = User | Post

        type User {
            id: ID!
            name: String!
        }

        type Post {
            id: ID!
            title: String!
        }

        type Query {
            search(q: String!): SearchResult
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            query Search($q: String!) {
                search(q: $q) {
                    ... on User {
                        id
                        name
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

    with pytest.raises(
        ValueError,
        match=r"Missing __typename in selection set for union 'SearchResult'",
    ):
        test_project.generate()


def test_union_fragment_typename_in_variants(test_project: ProjectBuilder):
    schema = """
        union SearchResult = User | Post

        type User {
            id: ID!
            name: String!
        }

        type Post {
            id: ID!
            title: String!
        }

        type Query {
            search(q: String!): SearchResult
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            query Search($q: String!) {
                search(q: $q) {
                    ... on User {
                        __typename
                        id
                        name
                    }
                    ... on Post {
                        __typename
                        id
                        title
                    }
                }
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True


async def test_nullable_union_result_validation(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            node(id: ID!): Node
        }

        union Node = User | Admin

        type User {
            id: ID!
            name: String!
        }

        type Admin {
            id: ID!
            permissions: [String!]!
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        get_node = api_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    __typename
                    ... on User {
                        id
                        name
                    }
                    ... on Admin {
                        id
                        permissions
                    }
                }
            }
            '''
        )
    """

    def resolve_node(_root, _info, *, id: str):
        if id == "none":
            return None
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Morty"}
        return {"__typename": "Admin", "id": id, "permissions": ["portal"]}

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"node": resolve_node}},
    ) as (api, queries):
        none_result = await queries.get_node.execute(id="none")
        assert none_result.node is None

        user_result = await queries.get_node.execute(id="user-1")
        assert isinstance(user_result.node, api.GetNodeResultNodeUser)
        assert user_result.node.name == "Morty"

        admin_result = await queries.get_node.execute(id="admin-1")
        assert isinstance(admin_result.node, api.GetNodeResultNodeAdmin)
        assert admin_result.node.permissions == ["portal"]


async def test_list_wrapped_union(test_project: ProjectBuilder, httpserver: HTTPServer):
    schema = """
        type Query {
            nodes: [Node!]!
        }

        union Node = User | Post

        type User {
            id: ID!
            name: String!
        }

        type Post {
            id: ID!
            title: String!
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        get_nodes = api_gql(
            '''
            query GetNodes {
                nodes {
                    __typename
                    ... on User {
                        id
                        name
                    }
                    ... on Post {
                        id
                        title
                    }
                }
            }
            '''
        )
    """

    def resolve_nodes(_root, _info):
        return [
            {"__typename": "User", "id": "u-1", "name": "Morty"},
            {"__typename": "Post", "id": "p-1", "title": "GraphQL 101"},
        ]

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"nodes": resolve_nodes}},
    ) as (api, queries):
        result = await queries.get_nodes.execute()
        assert len(result.nodes) == 2
        assert isinstance(result.nodes[0], api.GetNodesResultNodesUser)
        assert result.nodes[0].name == "Morty"
        assert isinstance(result.nodes[1], api.GetNodesResultNodesPost)
        assert result.nodes[1].title == "GraphQL 101"


def test_interface_exhaustively_covered(test_project: ProjectBuilder):
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
            node: Node
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
            '''
            query GetNode {
                node {
                    __typename
                    ... on User {
                        id
                        name
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

    assert test_project.generate() is True
    # We verify that no "Node" fallback model is generated or used,
    # and the union is just User | Post
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "GetNodeResultNodePost | GetNodeResultNodeUser" in generated
    assert "GetNodeResultNodeNode" not in generated
