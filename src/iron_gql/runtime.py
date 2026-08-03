import json
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import MutableMapping
from contextlib import asynccontextmanager
from contextlib import contextmanager
from typing import IO
from typing import Any
from typing import Self
from typing import TypeIs

import httpx2
import pydantic
from httpx2.websockets import ASGIWebSocketTransport
from httpx2.websockets import AsyncWebSocketClient
from httpx2.websockets import WebSocketClient

from iron_gql.errors import GraphQLResponseError
from iron_gql.slots import SlotFragments
from iron_gql.websockets import async_graphql_ws_subscribe
from iron_gql.websockets import graphql_ws_subscribe
from iron_gql.websockets import ws_url

DEFAULT_QUERY_TIMEOUT = 10

ASGIApp = Callable[
    [
        MutableMapping[str, Any],
        Callable[[], Awaitable[MutableMapping[str, Any]]],
        Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


class FileVar:
    """A file to be uploaded via GraphQL multipart request."""

    def __init__(
        self,
        f: IO[bytes] | bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ):
        """Args:
        f: File-like object opened in binary mode, or raw bytes.
        filename: Name sent to the server; defaults to a numeric index.
        content_type: MIME type; when omitted, httpx2 infers it.
        """
        self.f = f
        self.filename = filename
        self.content_type = content_type


class _ResponseBody(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore")
    data: dict[str, Any] | None = None
    errors: list[dict[str, Any]] | None = None


class GQLOperation:
    def __init__(self):
        self.headers: dict[str, str] = {}

    def with_headers(self, headers: dict[str, str]) -> Self:
        q = self.__class__()
        q.headers = dict(headers)
        return q


def _build_payload(
    query: str, variables: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, FileVar]]:
    payload: dict[str, Any] = {"query": query}
    serialized_vars, files = serialize_variables(variables)
    if serialized_vars:
        payload["variables"] = serialized_vars
    return payload, files


def _multipart_body(
    payload: dict[str, Any], files: dict[str, FileVar]
) -> tuple[dict[str, str], dict[str, tuple[str, Any] | tuple[str, Any, str]]]:
    file_map: dict[str, list[str]] = {}
    file_streams: dict[str, tuple[str, Any] | tuple[str, Any, str]] = {}

    for i, (path, file_var) in enumerate(files.items()):
        key = str(i)
        file_map[key] = [path]
        name = file_var.filename or key
        if file_var.content_type:
            file_streams[key] = (name, file_var.f, file_var.content_type)
        else:
            file_streams[key] = (name, file_var.f)

    body = {"operations": json.dumps(payload), "map": json.dumps(file_map)}
    return body, file_streams


def _parse_query_response[T: pydantic.BaseModel](
    response: httpx2.Response,
    result_type: type[T],
    slot_fragments: SlotFragments | None,
) -> T:
    if httpx2.codes.is_redirect(response.status_code):
        # httpx2 `Headers.get` is typed as Any
        location: str = response.headers.get("Location", "")  # pyright: ignore[reportAny]
        message = f"Unexpected 3xx response ({response.status_code})"
        if location:
            message = f"{message} to {location}"
        raise httpx2.HTTPStatusError(
            message, request=response.request, response=response
        )
    response.raise_for_status()
    try:
        body = _ResponseBody.model_validate(response.json())
    except pydantic.ValidationError as exc:
        raise GraphQLResponseError([
            {"message": f"Malformed response body: {exc}"}
        ]) from exc

    if body.errors:
        raise GraphQLResponseError(body.errors)
    if body.data is None:
        raise GraphQLResponseError([{"message": "No data in response"}])

    return result_type.model_validate(body.data, context=slot_fragments)


class AsyncGQLClient:
    def __init__(
        self,
        *,
        base_url: str,
        target_app: ASGIApp | None = None,
        headers: dict[str, str] | None = None,
        query_timeout: int = DEFAULT_QUERY_TIMEOUT,
    ):
        self._endpoint_url = httpx2.URL(base_url)
        self._target_app = target_app
        transport = httpx2.ASGITransport(app=target_app) if target_app else None
        self._client = httpx2.AsyncClient(
            transport=transport,
            headers=headers or {},
            timeout=query_timeout,
        )

    async def query[T: pydantic.BaseModel](
        self,
        result_type: type[T],
        query: str,
        *,
        variables: dict[str, Any],
        headers: dict[str, str],
        slot_fragments: SlotFragments | None = None,
    ) -> T:
        payload, files = _build_payload(query, variables)
        if files:
            body, file_streams = _multipart_body(payload, files)
            response = await self._client.post(
                self._endpoint_url, data=body, files=file_streams, headers=headers
            )
        else:
            response = await self._client.post(
                self._endpoint_url, json=payload, headers=headers
            )
        return _parse_query_response(response, result_type, slot_fragments)

    @asynccontextmanager
    async def subscribe[T: pydantic.BaseModel](
        self,
        result_type: type[T],
        query: str,
        *,
        variables: dict[str, Any],
        headers: dict[str, str],
        slot_fragments: SlotFragments | None = None,
    ) -> AsyncGenerator[AsyncGenerator[T]]:
        serialized_vars, files = serialize_variables(variables)
        if files:
            msg = "File uploads are not supported in subscriptions"
            raise TypeError(msg)
        transport = (
            ASGIWebSocketTransport(self._target_app) if self._target_app else None
        )
        url = str(ws_url(self._endpoint_url))
        try:
            async with (
                httpx2.AsyncClient(
                    transport=transport,
                    headers={
                        **httpx2.Headers(self._client.headers),
                        **headers,
                    },
                    cookies=httpx2.Cookies(self._client.cookies),
                ) as client,
                AsyncWebSocketClient(client).connect(
                    url, subprotocols=["graphql-transport-ws"]
                ) as ws,
                async_graphql_ws_subscribe(
                    ws, result_type, query, serialized_vars, slot_fragments
                ) as stream,
            ):
                yield stream
        except BaseExceptionGroup as eg:
            # httpx2.websockets wraps exceptions in nested ExceptionGroups via task
            # groups; unwrap so callers see plain exceptions (except* always re-wraps)
            unwrapped = _unwrap_solo_exception_group(eg)
            if unwrapped is not eg:
                raise unwrapped from None
            raise

    async def close(self) -> None:
        await self._client.aclose()


class GQLClient:
    def __init__(
        self,
        *,
        base_url: str,
        headers: dict[str, str] | None = None,
        query_timeout: int = DEFAULT_QUERY_TIMEOUT,
    ):
        self._endpoint_url = httpx2.URL(base_url)
        self._client = httpx2.Client(headers=headers or {}, timeout=query_timeout)

    def query[T: pydantic.BaseModel](
        self,
        result_type: type[T],
        query: str,
        *,
        variables: dict[str, Any],
        headers: dict[str, str],
        slot_fragments: SlotFragments | None = None,
    ) -> T:
        payload, files = _build_payload(query, variables)
        if files:
            body, file_streams = _multipart_body(payload, files)
            response = self._client.post(
                self._endpoint_url, data=body, files=file_streams, headers=headers
            )
        else:
            response = self._client.post(
                self._endpoint_url, json=payload, headers=headers
            )
        return _parse_query_response(response, result_type, slot_fragments)

    @contextmanager
    def subscribe[T: pydantic.BaseModel](
        self,
        result_type: type[T],
        query: str,
        *,
        variables: dict[str, Any],
        headers: dict[str, str],
        slot_fragments: SlotFragments | None = None,
    ) -> Generator[Generator[T]]:
        serialized_vars, files = serialize_variables(variables)
        if files:
            msg = "File uploads are not supported in subscriptions"
            raise TypeError(msg)
        url = str(ws_url(self._endpoint_url))
        # The sync session reuses the query client, so its default headers and
        # cookies already apply; the async client has to rebuild them because
        # its websocket transport differs from its HTTP one.
        with (
            WebSocketClient(self._client).connect(
                url, subprotocols=["graphql-transport-ws"], headers=headers
            ) as ws,
            graphql_ws_subscribe(
                ws, result_type, query, serialized_vars, slot_fragments
            ) as stream,
        ):
            yield stream

    def close(self) -> None:
        self._client.close()


# typeshed quirk: `BaseExceptionGroup.exceptions` is declared via a Self-bound
# TypeVar, which pyright widens to `… | Unknown` whenever the group is
# parameterized with anything broader than `Exception` — including, recursively,
# the same group type passed back to this function. The local ignores name the
# specific stub limitation rather than asserting "we know better".
def _unwrap_solo_exception_group(
    eg: BaseExceptionGroup[BaseException],
) -> BaseException:
    nested: tuple[BaseException, ...] = eg.exceptions
    if len(nested) != 1:
        return eg
    inner = nested[0]
    if isinstance(inner, BaseExceptionGroup):
        return _unwrap_solo_exception_group(inner)
    return inner


_adapter_cache: dict[type[object], pydantic.TypeAdapter[object]] = {}

# Recursive sum of values walk() understands structurally. Anything outside
# this set is delegated to a pydantic TypeAdapter in `walk`.
type _Walkable = (
    bool
    | int
    | float
    | str
    | FileVar
    | pydantic.BaseModel
    | list[object]
    | tuple[object, ...]
    | dict[str, object]
    | None
)

_WALKABLE_TYPES: tuple[type[object], ...] = (
    type(None),
    bool,
    int,
    float,
    str,
    FileVar,
    pydantic.BaseModel,
    list,
    tuple,
    dict,
)


def _is_walkable(obj: object) -> TypeIs[_Walkable]:
    return isinstance(obj, _WALKABLE_TYPES)


def serialize_variables(
    variables: Mapping[str, object],
) -> tuple[dict[str, Any], dict[str, FileVar]]:
    files: dict[str, FileVar] = {}

    def walk(path: str, obj: object) -> object:
        if _is_walkable(obj):
            return _walk_known(path, obj)
        tp = type(obj)
        if tp not in _adapter_cache:
            _adapter_cache[tp] = pydantic.TypeAdapter(tp)
        # pydantic `dump_python` is typed as Any
        return _adapter_cache[tp].dump_python(obj, mode="json")  # pyright: ignore[reportAny]

    def _walk_known(path: str, obj: _Walkable) -> object:
        match obj:
            case FileVar():
                files[path] = obj
                return None
            case None | str() | bool() | int() | float():
                return obj
            case list() | tuple():
                return [walk(f"{path}.{i}", v) for i, v in enumerate(obj)]
            case dict():
                return {k: walk(f"{path}.{k}", v) for k, v in obj.items()}
            case pydantic.BaseModel():
                dumped = obj.model_dump(
                    mode="python", by_alias=True, exclude_unset=True
                )
                return walk(path, dumped)

    # walk preserves dicts as dicts and the input is a str-keyed dict, so the
    # top-level result has the same shape — but `walk`'s return type is
    # `object` because nested branches can produce primitives. Pin it here
    # rather than route every recursive return through a generic.
    serialized: dict[str, object] = walk("variables", dict(variables))  # pyright: ignore[reportAssignmentType]
    return serialized, files
