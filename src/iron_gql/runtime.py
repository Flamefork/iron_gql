import json
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from contextlib import asynccontextmanager
from typing import IO
from typing import Any
from typing import Self

import httpx
import pydantic
from httpx_ws import aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport

from iron_gql.errors import GraphQLResponseError
from iron_gql.websockets import graphql_ws_subscribe
from iron_gql.websockets import ws_url

DEFAULT_QUERY_TIMEOUT = 10

_ASGIApp = Callable[
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
        content_type: MIME type; when omitted, httpx infers it.
        """
        self.f = f
        self.filename = filename
        self.content_type = content_type


class GQLOperation:
    def __init__(self):
        self.headers: dict[str, str] = {}

    def _copy(self) -> Self:
        q = self.__class__()
        q.headers = dict(self.headers)
        return q

    def with_headers(self, headers: dict[str, str]) -> Self:
        q = self._copy()
        q.headers = dict(headers)
        return q


class GQLClient:
    def __init__(
        self,
        *,
        base_url: str,
        target_app: _ASGIApp | None = None,
        headers: dict[str, str] | None = None,
        query_timeout: int = DEFAULT_QUERY_TIMEOUT,
    ):
        self._endpoint_url = httpx.URL(base_url)
        self._target_app = target_app
        transport = httpx.ASGITransport(app=target_app) if target_app else None
        self._client = httpx.AsyncClient(
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
    ) -> T:
        payload: dict[str, Any] = {"query": query}
        sierialized_vars, files = serialize_variables(variables)
        if sierialized_vars:
            payload["variables"] = sierialized_vars
        if files:
            response = await self._post_multipart_request(payload, files, headers)
        else:
            response = await self._post_normal_request(payload, headers)

        if httpx.codes.is_redirect(response.status_code):
            location = response.headers.get("Location")
            message = f"Unexpected 3xx response ({response.status_code})"
            if location:
                message = f"{message} to {location}"
            raise httpx.HTTPStatusError(
                message, request=response.request, response=response
            )
        response.raise_for_status()
        body = response.json()
        errors = body.get("errors")

        if errors:
            raise GraphQLResponseError(errors)
        if body.get("data") is None:
            raise GraphQLResponseError([{"message": "No data in response"}])

        return result_type.model_validate(body["data"])

    async def _post_normal_request(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        return await self._client.post(
            self._endpoint_url,
            json=payload,
            headers=headers,
        )

    async def _post_multipart_request(
        self,
        payload: dict[str, Any],
        files: dict[str, FileVar],
        headers: dict[str, str],
    ) -> httpx.Response:
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

        return await self._client.post(
            self._endpoint_url,
            data={
                "operations": json.dumps(payload),
                "map": json.dumps(file_map),
            },
            files=file_streams,
            headers=headers,
        )

    @asynccontextmanager
    async def subscribe[T: pydantic.BaseModel](
        self,
        result_type: type[T],
        query: str,
        *,
        variables: dict[str, Any],
        headers: dict[str, str],
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
                httpx.AsyncClient(
                    transport=transport,
                    headers={
                        **httpx.Headers(self._client.headers),
                        **headers,
                    },
                    cookies=httpx.Cookies(self._client.cookies),
                ) as client,
                aconnect_ws(url, client, subprotocols=["graphql-transport-ws"]) as ws,
                graphql_ws_subscribe(ws, result_type, query, serialized_vars) as stream,
            ):
                yield stream
        except BaseExceptionGroup as eg:
            # httpx-ws 0.9+ wraps exceptions in nested ExceptionGroups via task groups;
            # unwrap so callers see plain exceptions (except* always re-wraps)
            exc: BaseException = eg
            while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
                exc = exc.exceptions[0]
            if exc is not eg:
                raise exc from None
            raise

    async def close(self) -> None:
        await self._client.aclose()


_adapter_cache: dict[type, pydantic.TypeAdapter[object]] = {}


def serialize_variables(
    variables: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, FileVar]]:
    files: dict[str, FileVar] = {}

    def walk(path: str, obj: Any) -> Any:
        match obj:
            case FileVar():
                files[path] = obj
                return None
            case str() | int() | float() | bool() | None:
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
            case _:
                tp = type(obj)
                if tp not in _adapter_cache:
                    _adapter_cache[tp] = pydantic.TypeAdapter(tp)
                return _adapter_cache[tp].dump_python(obj, mode="json")

    serialized = walk("variables", variables)
    return serialized, files
