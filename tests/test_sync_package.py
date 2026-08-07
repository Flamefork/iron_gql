import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from iron_gql import GraphQLResponseError
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.testing import accept_graphql_ws
from iron_gql.testing.server import live_asgi_server
from tests.conftest import generated_package
from tests.conftest import sync_gql_server
from tests.conftest import use_sync_package_client

generated_package(
    "sync_package",
    mode="sync",
    schema="""
    type Query {
        user(id: ID!): User
    }

    type Mutation {
        renameUser(id: ID!, name: String!): User!
    }

    type Subscription {
        userRenamed(id: ID!): User!
    }

    type User {
        id: ID!
        name: String!
        manager: User
    }
    """,
    queries='''
    from tests.generated.sync_package.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($id: ID!) {
            user(id: $id) {
                id
                name
            }
        }
        """
    )

    rename_user = api_gql(
        """
        mutation RenameUser($id: ID!, $name: String!) {
            renameUser(id: $id, name: $name) {
                id
                name
            }
        }
        """
    )

    user_renamed = api_gql(
        """
        subscription UserRenamed($id: ID!) {
            userRenamed(id: $id) {
                id
                name
            }
        }
        """
    )

    # Finding F12: the renderer emits a distinct sync path for bindings (the
    # branch's real-world consumer is a sync package), but nothing calls
    # execute() on one -- this template/fragment/bind exercises exactly that.
    get_user_with_manager = api_gql(
        """
        query GetUserWithManager($id: ID!) {
            user(id: $id) {
                id
                manager @slot { __typename }
            }
        }
        """
    )

    manager_name = api_gql(
        """
        fragment ManagerName on User {
            name
        }
        """
    )

    bound_user_with_manager = get_user_with_manager.bind(manager=manager_name)
    ''',
)

from tests.generated.sync_package import queries as sync_queries


def test_sync_package_runs_query_and_mutation(httpserver: HTTPServer):
    state = {"user-1": "Graph"}

    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str] | None:
        name = state.get(id)
        if name is None:
            return None
        return {"id": id, "name": name}

    def resolve_rename_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str, name: str
    ) -> dict[str, str]:
        state[id] = name
        return {"id": id, "name": name}

    with sync_gql_server(
        httpserver,
        "sync_package",
        {
            "Query": {"user": resolve_user},
            "Mutation": {"renameUser": resolve_rename_user},
        },
    ):
        initial = sync_queries.get_user.with_headers({
            "Authorization": "Bearer token"
        }).execute(id="user-1")
        assert initial.user is not None
        assert initial.user.name == "Graph"

        renamed = sync_queries.rename_user.execute(id="user-1", name="Bob")
        assert renamed.rename_user.name == "Bob"

        missing = sync_queries.get_user.execute(id="user-2")
        assert missing.user is None


def test_sync_binding_executes_and_reads_the_slot(httpserver: HTTPServer):
    # Finding F12: no test anywhere called execute() on a sync-mode binding,
    # even though the renderer emits a distinct sync path for it and
    # example/gql/api_sync.py -- the branch's real-world consumer -- carries
    # one. `resolve_user` returns the manager nested in the same dict, so
    # graphql-core's default resolver reads it without a dedicated resolver.
    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, object]:
        return {
            "id": id,
            "name": "Graph",
            "manager": {"id": f"{id}-mgr", "name": "Carol"},
        }

    with sync_gql_server(httpserver, "sync_package", {"Query": {"user": resolve_user}}):
        result = sync_queries.bound_user_with_manager.execute(id="user-1")
        assert result.user is not None
        manager = sync_queries.manager_name.read(result.user.manager)
        assert manager is not None
        assert manager.name == "Carol"


def test_sync_package_raises_graphql_errors(httpserver: HTTPServer):
    def resolve_user(_root: None, _info: GraphQLResolveInfo, *, id: str) -> None:
        msg = f"no such user: {id}"
        raise RuntimeError(msg)

    with (
        sync_gql_server(httpserver, "sync_package", {"Query": {"user": resolve_user}}),
        pytest.raises(GraphQLResponseError, match="no such user"),
    ):
        sync_queries.get_user.execute(id="user-1")


def _renamed_ws_app(payloads: list[dict[str, object]]) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        connection = await accept_graphql_ws(scope, receive, send)
        subscription = await connection.ack()
        for payload in payloads:
            await subscription.next(payload)
        await subscription.complete()
        await connection.drain()

    return app


def test_sync_package_streams_subscription():
    app = _renamed_ws_app([
        {"userRenamed": {"id": "user-1", "name": "Bob"}},
        {"userRenamed": {"id": "user-1", "name": "Carol"}},
    ])
    with (
        live_asgi_server(app) as base_url,
        use_sync_package_client("sync_package", base_url),
        sync_queries.user_renamed.execute(id="user-1") as stream,
    ):
        names = [event.user_renamed.name for event in stream]

    assert names == ["Bob", "Carol"]
