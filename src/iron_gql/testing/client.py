from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from contextlib import AbstractContextManager
from contextlib import asynccontextmanager
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from types import ModuleType
from typing import cast
from typing import overload

from iron_gql.runtime import AsyncGQLClient
from iron_gql.runtime import GQLClient

type _Client = GQLClient | AsyncGQLClient


# One per `use_client` entry rather than per client: identity tells two
# entries apart even when they were handed the same client.
@dataclass(eq=False)
class _Swap:
    client: _Client


@dataclass
class _ModuleSwaps:
    original: _Client
    active: list[_Swap] = field(default_factory=list[_Swap])


# Kept here, not in the generated module's namespace: a module carries only
# what the generator wrote into it.
_swaps: dict[ModuleType, _ModuleSwaps] = {}


def _enter(api_module: ModuleType, client: _Client) -> _Swap:
    namespace = cast("dict[str, object]", vars(api_module))
    swaps = _swaps.get(api_module)
    if swaps is None:
        # The module's first swap: what it binds now is its own client.
        swaps = _ModuleSwaps(cast("_Client", namespace["_client"]))
    # Before anything is recorded: a refused client stays its caller's.
    client.adopt_model_base(swaps.original.model_base)
    swap = _Swap(client)
    swaps.active.append(swap)
    _swaps[api_module] = swaps
    namespace["_client"] = client
    return swap


# Removes `swap` wherever it stands and binds the latest swap left, or the
# original client once none is. Returns whether `swap` was the latest one.
def _leave(api_module: ModuleType, swap: _Swap) -> bool:
    namespace = cast("dict[str, object]", vars(api_module))
    swaps = _swaps[api_module]
    latest = swaps.active[-1] is swap
    swaps.active.remove(swap)
    if swaps.active:
        namespace["_client"] = swaps.active[-1].client
    else:
        namespace["_client"] = swaps.original
        del _swaps[api_module]
    return latest


def _out_of_order(api_module: ModuleType) -> RuntimeError:
    msg = (
        f"use_client of {api_module.__name__} exited while a use_client of the "
        "same module entered after it is still active. Nest the swaps of one "
        "module, and do not swap one module from tests that run at the same "
        "time in one process. The module's client was restored as far as "
        "possible, but from now on there are no guarantees"
    )
    return RuntimeError(msg)


# Binds `client` into a generated module for the duration of a test. The swap
# holds for the whole process, worker threads included; isolating requests of
# scenarios that run at the same time is not promised. Swaps of one module
# nest: on the way out each restores the client bound before it and closes its
# own, so each takes a client instance of its own. One that exits while a
# later one is still active raises `RuntimeError` in the offending test; the
# binding is then restored on a best-effort basis, without guarantees. Entering
# and exiting swaps of one module from several threads at once is not
# supported: there is no lock. A client built without a model base parses on
# the module's; one built with another is refused and stays the caller's to
# close. The binding moves before the client closes, and, in the async manager,
# before the first `await`.
@contextmanager
def _sync_client_context(
    api_module: ModuleType, client: GQLClient
) -> Iterator[GQLClient]:
    swap = _enter(api_module, client)
    try:
        yield client
    finally:
        latest = _leave(api_module, swap)
        client.close()
        if not latest:
            raise _out_of_order(api_module)


@asynccontextmanager
async def _async_client_context(
    api_module: ModuleType, client: AsyncGQLClient
) -> AsyncIterator[AsyncGQLClient]:
    swap = _enter(api_module, client)
    try:
        yield client
    finally:
        latest = _leave(api_module, swap)
        await client.close()
        if not latest:
            raise _out_of_order(api_module)


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
