import pytest
from gql.transport.exceptions import TransportQueryError
from pytest_httpserver import HTTPServer

from iron_gql.runtime import GQLQuery
from iron_gql.runtime import serialize_var
from tests.conftest import ProjectBuilder


async def test_generate_and_execute_queries(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
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

    query_source = """
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            query GetUser($id: ID!) {
                user(id: $id) {
                    id
                    name
                }
            }
            '''
        )

        update_user = api_gql(
            '''
            mutation UpdateUser($input: UpdateUserInput!) {
                updateUser(input: $input) {
                    id
                    name
                }
            }
            '''
        )
    """

    state = {"user-1": "Graph"}

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

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={
            "Query": {"user": resolve_user},
            "Mutation": {"updateUser": resolve_update_user},
        },
    ) as (api, queries):
        read_query = queries.get_user.with_headers({"Authorization": "Bearer token"})
        initial = await read_query.execute(id="user-1")
        assert initial.user is not None
        assert initial.user.name == "Graph"

        mutation_input = api.UpdateUserInput(id="user-1", name="Morty")
        updated = await queries.update_user.execute(input=mutation_input)
        assert updated.update_user.name == "Morty"
        refreshed = await queries.get_user.execute(id="user-1")
        assert refreshed.user is not None
        assert refreshed.user.name == "Morty"


async def test_list_allows_null_elements(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            numbers1: [Int]!
            numbers2: [Int!]
        }
    """

    call_count = 0

    def resolve_numbers1(_root, _info):
        nonlocal call_count
        if call_count == 0:
            return [1, None]  # valid: [Int]! allows null elements
        return [1, 2]

    def resolve_numbers2(_root, _info):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [1, 2]  # valid: [Int!] doesn't allow null elements
        return [1, None]  # invalid: null in [Int!]

    with test_project.server(
        httpserver,
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        numbers = api_gql(
            '''
            query Numbers {
                numbers1
                numbers2
            }
            '''
        )
        """,
        resolvers={
            "Query": {"numbers1": resolve_numbers1, "numbers2": resolve_numbers2}
        },
    ) as (_, queries):
        response = await queries.numbers.execute()
        assert response.numbers_1 == [1, None]
        assert response.numbers_2 == [1, 2]

        with pytest.raises(TransportQueryError):
            await queries.numbers.execute()


async def test_variable_defaults_optional(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            posts(limit: Int = 5): [Int!]!
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        get_posts = api_gql(
            '''
            query GetPosts($limit: Int = 5) {
                posts(limit: $limit)
            }
            '''
        )
    """

    def resolve_posts(_root, _info, *, limit: int = 5):
        return list(range(limit))

    with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"posts": resolve_posts}},
    ) as (_, queries):
        default_result = await queries.get_posts.execute()
        assert default_result.posts == [0, 1, 2, 3, 4]

        explicit_result = await queries.get_posts.execute(limit=2)
        assert explicit_result.posts == [0, 1]


def test_serialize_var_handles_nested_structures():
    nested_list = [[1, 2], [3, 4]]
    assert serialize_var(nested_list) == [[1, 2], [3, 4]]

    nested_dict = {"a": {"b": 1}, "c": [1, 2]}
    assert serialize_var(nested_dict) == {"a": {"b": 1}, "c": [1, 2]}

    mixed = {"a": [1, {"b": 2}]}
    assert serialize_var(mixed) == {"a": [1, {"b": 2}]}


def test_query_with_file_uploads():
    query = GQLQuery()
    assert query.upload_files is False

    new_query = query.with_file_uploads()
    assert new_query.upload_files is True
    assert query.upload_files is False  # Original unchanged
