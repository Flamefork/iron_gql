import re

from pytest_httpserver import HTTPServer

from tests.conftest import ProjectBuilder


async def test_inline_fragment_without_type_condition(
    test_project: ProjectBuilder, httpserver: HTTPServer
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

    query_source = """
        from sample_app.gql.api import api_gql

        get_viewer = api_gql(
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
    """

    def resolve_viewer(_root, _info):
        return {"id": "user-1", "name": "Morty", "email": "morty@example.com"}

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"viewer": resolve_viewer}},
    ) as (_, queries):
        result = await queries.get_viewer.execute()
        assert result.viewer.id == "user-1"
        assert result.viewer.name == "Morty"
        assert result.viewer.email == "morty@example.com"


async def test_named_fragments(test_project: ProjectBuilder, httpserver: HTTPServer):
    schema = """
        type User {
            id: ID!
            name: String!
            email: String
        }

        type Query {
            user(id: ID!): User
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        user_fragment = api_gql(
            '''
            fragment UserFields on User {
                id
                name
            }
            '''
        )

        get_user = api_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    ...UserFields
                    email
                }
            }
            '''
        )
    """

    def resolve_user(_root, _info, *, id: str):
        return {"id": id, "name": "Morty", "email": "morty@example.com"}

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"user": resolve_user}},
    ) as (_, queries):
        result = await queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"
        assert result.user.name == "Morty"
        assert result.user.email == "morty@example.com"


async def test_duplicate_fragment_names_use_local_definition(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type User {
            id: ID!
            name: String!
        }

        type Query {
            user(id: ID!): User
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            fragment UserFields on User {
                id
                name
            }

            query GetUser($id: ID!) {
                user(id: $id) {
                    ...UserFields
                }
            }
            '''
        )

        other_fragment = api_gql(
            '''
            fragment UserFields on User {
                id
            }
            '''
        )
    """

    def resolve_user(_root, _info, *, id: str):
        return {"id": id, "name": "Morty"}

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"user": resolve_user}},
    ) as (_, queries):
        result = await queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"
        assert result.user.name == "Morty"


async def test_fragment_validation_scoped_to_query(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
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
    """

    query_source = """
        from sample_app.gql.api import api_gql

        user_fragment = api_gql(
            '''
            fragment UserFields on User {
                id
                name
            }
            '''
        )

        post_fragment = api_gql(
            '''
            fragment PostFields on Post {
                id
                title
            }
            '''
        )

        get_user = api_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    ...UserFields
                }
            }
            '''
        )

        get_post = api_gql(
            '''
            query GetPost($id: ID!) {
                post(id: $id) {
                    ...PostFields
                }
            }
            '''
        )
    """

    def resolve_user(_root, _info, *, id: str):
        return {"id": id, "name": "Morty"}

    def resolve_post(_root, _info, *, id: str):
        return {"id": id, "title": "GraphQL 101"}

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"user": resolve_user, "post": resolve_post}},
    ) as (_, queries):
        user_result = await queries.get_user.execute(id="u-1")
        assert user_result.user is not None
        assert user_result.user.id == "u-1"
        assert user_result.user.name == "Morty"

        post_result = await queries.get_post.execute(id="p-1")
        assert post_result.post is not None
        assert post_result.post.id == "p-1"
        assert post_result.post.title == "GraphQL 101"


async def test_inline_fragment_definitions_not_duplicated(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type User {
            id: ID!
            name: String!
        }

        type Query {
            user(id: ID!): User
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            fragment UserFields on User {
                id
                name
            }

            query GetUser($id: ID!) {
                user(id: $id) {
                    ...UserFields
                }
            }
            '''
        )
    """

    def resolve_user(_root, _info, *, id: str):
        return {"id": id, "name": "Morty"}

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"user": resolve_user}},
    ) as (_, queries):
        result = await queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"
        assert result.user.name == "Morty"


async def test_transitive_fragments(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    """Test that nested fragment deps (A → B → C) are resolved correctly."""
    schema = """
        type User {
            id: ID!
            name: String!
            email: String
            role: String!
        }

        type Query {
            user(id: ID!): User
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        fragment_c = api_gql(
            '''
            fragment RoleFields on User {
                role
            }
            '''
        )

        fragment_b = api_gql(
            '''
            fragment ContactFields on User {
                email
                ...RoleFields
            }
            '''
        )

        fragment_a = api_gql(
            '''
            fragment UserFields on User {
                id
                name
                ...ContactFields
            }
            '''
        )

        get_user = api_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    ...UserFields
                }
            }
            '''
        )
    """

    def resolve_user(_root, _info, *, id: str):
        return {
            "id": id,
            "name": "Morty",
            "email": "morty@example.com",
            "role": "admin",
        }

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"user": resolve_user}},
    ) as (_, queries):
        result = await queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"
        assert result.user.name == "Morty"
        assert result.user.email == "morty@example.com"
        assert result.user.role == "admin"


async def test_exec_source_contains_expanded_fragments(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    """Verify that the request string contains expanded fragment definitions."""
    schema = """
        type User {
            id: ID!
            name: String!
        }

        type Query {
            user(id: ID!): User
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        user_fragment = api_gql(
            '''
            fragment UserFields on User {
                id
                name
            }
            '''
        )

        get_user = api_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    ...UserFields
                }
            }
            '''
        )
    """

    def resolve_user(_root, _info, *, id: str):
        return {"id": id, "name": "Morty"}

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"user": resolve_user}},
    ) as (_, queries):
        # Read the generated code from file
        generated_code = (test_project.root / "sample_app/gql/api.py").read_text()

        # Verify the generated code contains the fragment definition in the request
        assert "fragment UserFields on User" in generated_code
        # The fragment should appear as part of the gql.gql() call
        assert re.search(r"gql\.gql\(['\"].*fragment UserFields", generated_code)

        # Also verify execution works
        result = await queries.get_user.execute(id="u-1")
        assert result.user is not None
        assert result.user.id == "u-1"
