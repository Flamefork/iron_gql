import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.conftest import gql_server

generated_package(
    "fragments_inline",
    schema="""
    type Query {
        viewer: User!
    }

    type User {
        id: ID!
        name: String!
        email: String!
    }
    """,
    queries='''
    from tests.generated.fragments_inline.gql.api import api_gql

    get_viewer = api_gql(
        """
        query GetViewer {
            viewer {
                id
                ... {
                    name
                    email
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "fragments_named",
    schema="""
    type User {
        id: ID!
        name: String!
        email: String
    }

    type Query {
        user(id: ID!): User
    }
    """,
    queries='''
    from tests.generated.fragments_named.gql.api import api_gql

    user_fragment = api_gql(
        """
        fragment UserFields on User {
            id
            name
        }
        """
    )

    get_user = api_gql(
        """
        query GetUser($id: ID!) {
            user(id: $id) {
                ...UserFields
                email
            }
        }
        """
    )
    ''',
)

generated_package(
    "fragments_dup_names",
    schema="""
    type User {
        id: ID!
        name: String!
    }

    type Query {
        user(id: ID!): User
    }
    """,
    queries='''
    from tests.generated.fragments_dup_names.gql.api import api_gql

    get_user = api_gql(
        """
        fragment UserFields on User {
            id
            name
        }

        query GetUser($id: ID!) {
            user(id: $id) {
                ...UserFields
            }
        }
        """
    )

    other_fragment = api_gql(
        """
        fragment UserFields on User {
            id
        }
        """
    )
    ''',
)

generated_package(
    "fragments_scoped",
    schema="""
    type User {
        id: ID!
        name: String!
    }

    type Post {
        id: ID!
        title: String!
    }

    type Query {
        user(id: ID!): User
        post(id: ID!): Post
    }
    """,
    queries='''
    from tests.generated.fragments_scoped.gql.api import api_gql

    user_fragment = api_gql(
        """
        fragment UserFields on User {
            id
            name
        }
        """
    )

    post_fragment = api_gql(
        """
        fragment PostFields on Post {
            id
            title
        }
        """
    )

    get_user = api_gql(
        """
        query GetUser($id: ID!) {
            user(id: $id) {
                ...UserFields
            }
        }
        """
    )

    get_post = api_gql(
        """
        query GetPost($id: ID!) {
            post(id: $id) {
                ...PostFields
            }
        }
        """
    )
    ''',
)

generated_package(
    "fragments_no_dup",
    schema="""
    type User {
        id: ID!
        name: String!
    }

    type Query {
        user(id: ID!): User
    }
    """,
    queries='''
    from tests.generated.fragments_no_dup.gql.api import api_gql

    get_user = api_gql(
        """
        fragment UserFields on User {
            id
            name
        }

        query GetUser($id: ID!) {
            user(id: $id) {
                ...UserFields
            }
        }
        """
    )
    ''',
)

generated_package(
    "fragments_transitive",
    schema="""
    type User {
        id: ID!
        name: String!
        email: String
        role: String!
    }

    type Query {
        user(id: ID!): User
    }
    """,
    queries='''
    from tests.generated.fragments_transitive.gql.api import api_gql

    fragment_c = api_gql(
        """
        fragment RoleFields on User {
            role
        }
        """
    )

    fragment_b = api_gql(
        """
        fragment ContactFields on User {
            email
            ...RoleFields
        }
        """
    )

    fragment_a = api_gql(
        """
        fragment UserFields on User {
            id
            name
            ...ContactFields
        }
        """
    )

    get_user = api_gql(
        """
        query GetUser($id: ID!) {
            user(id: $id) {
                ...UserFields
            }
        }
        """
    )
    ''',
)

generated_package(
    "fragments_exec_source",
    schema="""
    type User {
        id: ID!
        name: String!
    }

    type Query {
        user(id: ID!): User
    }
    """,
    queries='''
    from tests.generated.fragments_exec_source.gql.api import api_gql

    user_fragment = api_gql(
        """
        fragment UserFields on User {
            id
            name
        }
        """
    )

    get_user = api_gql(
        """
        query GetUser($id: ID!) {
            user(id: $id) {
                ...UserFields
            }
        }
        """
    )
    ''',
)

from tests.generated.fragments_dup_names import queries as dup_names_queries
from tests.generated.fragments_exec_source import queries as exec_source_queries
from tests.generated.fragments_inline import queries as inline_queries
from tests.generated.fragments_named import queries as named_queries
from tests.generated.fragments_no_dup import queries as no_dup_queries
from tests.generated.fragments_scoped import queries as scoped_queries
from tests.generated.fragments_transitive import queries as transitive_queries


async def test_inline_fragment_without_type_condition(httpserver: HTTPServer):
    def resolve_viewer(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob", "email": "bob@example.com"}

    async with gql_server(
        httpserver,
        "fragments_inline",
        {"Query": {"viewer": resolve_viewer}},
    ):
        result = await inline_queries.get_viewer.execute()
        assert result.viewer.id == "user-1"
        assert result.viewer.name == "Bob"
        assert result.viewer.email == "bob@example.com"


async def test_named_fragments(httpserver: HTTPServer):
    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        return {"id": id, "name": "Bob", "email": "bob@example.com"}

    async with gql_server(
        httpserver,
        "fragments_named",
        {"Query": {"user": resolve_user}},
    ):
        result = await named_queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"
        assert result.user.name == "Bob"
        assert result.user.email == "bob@example.com"


async def test_duplicate_fragment_names_use_local_definition(httpserver: HTTPServer):
    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        return {"id": id, "name": "Bob"}

    async with gql_server(
        httpserver,
        "fragments_dup_names",
        {"Query": {"user": resolve_user}},
    ):
        result = await dup_names_queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"
        assert result.user.name == "Bob"


async def test_fragment_validation_scoped_to_query(httpserver: HTTPServer):
    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        return {"id": id, "name": "Bob"}

    def resolve_post(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        return {"id": id, "title": "GraphQL 101"}

    async with gql_server(
        httpserver,
        "fragments_scoped",
        {"Query": {"user": resolve_user, "post": resolve_post}},
    ):
        user_result = await scoped_queries.get_user.execute(id="u-1")
        assert user_result.user is not None
        assert user_result.user.id == "u-1"
        assert user_result.user.name == "Bob"

        post_result = await scoped_queries.get_post.execute(id="p-1")
        assert post_result.post is not None
        assert post_result.post.id == "p-1"
        assert post_result.post.title == "GraphQL 101"


async def test_inline_fragment_definitions_not_duplicated(httpserver: HTTPServer):
    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        return {"id": id, "name": "Bob"}

    async with gql_server(
        httpserver,
        "fragments_no_dup",
        {"Query": {"user": resolve_user}},
    ):
        result = await no_dup_queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"
        assert result.user.name == "Bob"


async def test_transitive_fragments(httpserver: HTTPServer):
    """Test that nested fragment deps (A → B → C) are resolved correctly."""

    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        return {
            "id": id,
            "name": "Bob",
            "email": "bob@example.com",
            "role": "admin",
        }

    async with gql_server(
        httpserver,
        "fragments_transitive",
        {"Query": {"user": resolve_user}},
    ):
        result = await transitive_queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"
        assert result.user.name == "Bob"
        assert result.user.email == "bob@example.com"
        assert result.user.role == "admin"


async def test_exec_source_contains_expanded_fragments(httpserver: HTTPServer):
    """Verify that the request string contains expanded fragment definitions."""

    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        return {"id": id, "name": "Bob"}

    async with gql_server(
        httpserver,
        "fragments_exec_source",
        {"Query": {"user": resolve_user}},
    ):
        result = await exec_source_queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"


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
