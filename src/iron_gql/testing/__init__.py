"""Test helpers for consumers of a generated package.

Split by dependency: what this module re-exports needs nothing beyond the
iron_gql runtime, so a project that only swaps clients or scripts a
subscription fake installs plain `iron-gql`. `iron_gql.testing.server` runs a
loopback server and needs `iron-gql[testing]` for uvicorn.
"""

from iron_gql.testing.client import use_async_client
from iron_gql.testing.client import use_sync_client
from iron_gql.testing.ws import SUBPROTOCOL
from iron_gql.testing.ws import WSTestConnection
from iron_gql.testing.ws import WSTestSubscription
from iron_gql.testing.ws import accept_graphql_ws

__all__ = [
    "SUBPROTOCOL",
    "WSTestConnection",
    "WSTestSubscription",
    "accept_graphql_ws",
    "use_async_client",
    "use_sync_client",
]
