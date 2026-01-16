from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest_httpserver import HTTPServer
from pytest_httpserver.httpserver import HandlerType

from iron_gql.conftest import generate_and_import
from iron_gql.conftest import prepare_workspace
from iron_gql.conftest import sample_app_context
from iron_gql.conftest import setup_test_server


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

    with setup_test_server(
        tmp_path,
        httpserver,
        schema,
        query_source,
        {
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
        from sample_app.gql.api import api_gql

        NUMBERS = api_gql(
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

    with sample_app_context(tmp_path):
        api, queries = generate_and_import(tmp_path)

        httpserver.expect_request(
            "/graphql/",
            method="POST",
            handler_type=HandlerType.ONESHOT,
        ).respond_with_json({"data": {"numbers1": [1, None], "numbers2": [1, 2]}})

        api.API_CLIENT.base_url = httpserver.url_for("/graphql/")

        response = await queries.NUMBERS.execute()
        assert response.numbers_1 == [1, None]
        assert response.numbers_2 == [1, 2]

        httpserver.expect_request(
            "/graphql/",
            method="POST",
            handler_type=HandlerType.ONESHOT,
        ).respond_with_json({"data": {"numbers1": [1, 2], "numbers2": [1, None]}})

        with pytest.raises(ValidationError):
            await queries.NUMBERS.execute()


async def test_variable_defaults_optional(tmp_path: Path, httpserver: HTTPServer):
    schema = """
        type Query {
            posts(limit: Int = 5): [Int!]!
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        GET_POSTS = api_gql(
            '''
            query GetPosts($limit: Int = 5) {
                posts(limit: $limit)
            }
            '''
        )
    """

    def resolve_posts(_root, _info, *, limit: int = 5):
        return list(range(limit))

    with setup_test_server(
        tmp_path,
        httpserver,
        schema,
        query_source,
        {"Query": {"posts": resolve_posts}},
    ) as (_, queries):
        default_result = await queries.GET_POSTS.execute()
        assert default_result.posts == [0, 1, 2, 3, 4]

        explicit_result = await queries.GET_POSTS.execute(limit=2)
        assert explicit_result.posts == [0, 1]
