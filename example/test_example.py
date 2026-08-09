import pytest

from example import ch01_queries
from example import ch07_subscriptions
from example import ch08_sync
from example import fake_app
from example.generate import generate_packages
from example.gql import api
from example.gql import api_sync
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient
from iron_gql.testing import use_async_client
from iron_gql.testing import use_sync_client
from iron_gql.testing.server import live_asgi_server


async def test_fetch_user(capsys: pytest.CaptureFixture[str]):
    client = AsyncGQLClient(
        base_url="http://testserver/graphql/", target_app=fake_app.app
    )
    async with use_async_client(api, client):
        await ch01_queries.fetch_user("1")

    out = capsys.readouterr().out
    assert "Alice (alice@example.com), role: ADMIN" in out
    assert "  - Typed clients" in out


async def test_watch_new_posts(capsys: pytest.CaptureFixture[str]):
    client = AsyncGQLClient(
        base_url="http://testserver/graphql/", target_app=fake_app.app
    )
    async with use_async_client(api, client):
        await ch07_subscriptions.watch_new_posts("1")

    assert "New post: Slots, explained by Alice" in capsys.readouterr().out


def test_sync_fetch_user(capsys: pytest.CaptureFixture[str]):
    with (
        live_asgi_server(fake_app.app) as base_url,
        use_sync_client(api_sync, GQLClient(base_url=base_url)),
    ):
        ch08_sync.fetch_user("1")

    assert "Alice (alice@example.com), role: ADMIN" in capsys.readouterr().out


def test_generated_packages_are_current():
    assert generate_packages() == [False, False]
