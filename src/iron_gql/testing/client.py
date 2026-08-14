from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from contextlib import AbstractContextManager
from contextlib import asynccontextmanager
from contextlib import contextmanager
from types import ModuleType
from typing import cast
from typing import overload

from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient


# Binds `client` into a generated sync package for the duration of a test. The
# original client is restored and `client` is closed on the way out.
@contextmanager
def _sync_client_context(
    api_module: ModuleType, client: GQLClient
) -> Iterator[GQLClient]:
    namespace = cast("dict[str, object]", vars(api_module))
    previous = namespace["_client"]
    namespace["_client"] = client
    try:
        yield client
    finally:
        namespace["_client"] = previous
        client.close()


# The async counterpart of `_sync_client_context`.
@asynccontextmanager
async def _async_client_context(
    api_module: ModuleType, client: AsyncGQLClient
) -> AsyncIterator[AsyncGQLClient]:
    namespace = cast("dict[str, object]", vars(api_module))
    previous = namespace["_client"]
    namespace["_client"] = client
    try:
        yield client
    finally:
        namespace["_client"] = previous
        await client.close()


@overload
def use_client(
    api_module: ModuleType, client: GQLClient
) -> AbstractContextManager[GQLClient]: ...


@overload
def use_client(
    api_module: ModuleType, client: AsyncGQLClient
) -> AbstractAsyncContextManager[AsyncGQLClient]: ...


def use_client(
    api_module: ModuleType, client: GQLClient | AsyncGQLClient
) -> AbstractContextManager[GQLClient] | AbstractAsyncContextManager[AsyncGQLClient]:
    if isinstance(client, GQLClient):
        return _sync_client_context(api_module, client)
    return _async_client_context(api_module, client)
