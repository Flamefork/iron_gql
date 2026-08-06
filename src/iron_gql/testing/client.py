from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import asynccontextmanager
from contextlib import contextmanager
from types import ModuleType

from iron_gql.naming import client_binding_name
from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient


def _module_client_binding(api_module: ModuleType) -> str:
    return client_binding_name(api_module.__name__.rsplit(".", 1)[-1])


# Binds `client` into a generated sync package for the duration of a test. The
# original client is restored and `client` is closed on the way out.
@contextmanager
def use_sync_client(api_module: ModuleType, client: GQLClient) -> Iterator[GQLClient]:
    name = _module_client_binding(api_module)
    # Reading an attribute off a module is Any by construction.
    previous: object = getattr(api_module, name)  # pyright: ignore[reportAny]
    setattr(api_module, name, client)
    try:
        yield client
    finally:
        setattr(api_module, name, previous)
        client.close()


# The async counterpart of `use_sync_client`.
@asynccontextmanager
async def use_async_client(
    api_module: ModuleType, client: AsyncGQLClient
) -> AsyncIterator[AsyncGQLClient]:
    name = _module_client_binding(api_module)
    # Reading an attribute off a module is Any by construction.
    previous: object = getattr(api_module, name)  # pyright: ignore[reportAny]
    setattr(api_module, name, client)
    try:
        yield client
    finally:
        setattr(api_module, name, previous)
        await client.close()
