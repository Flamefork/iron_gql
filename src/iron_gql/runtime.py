import json
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import MutableMapping
from contextlib import asynccontextmanager
from contextlib import contextmanager
from copy import replace
from dataclasses import dataclass
from types import MappingProxyType
from typing import IO
from typing import Any
from typing import Self
from typing import TypeIs
from typing import cast

import httpx2
import pydantic
from httpx2.websockets import ASGIWebSocketTransport
from httpx2.websockets import AsyncWebSocketClient
from httpx2.websockets import WebSocketClient

from iron_gql.errors import GraphQLResponseError
from iron_gql.slots import OMITTED
from iron_gql.slots import FragmentDefinitionType
from iron_gql.slots import GQLBindableFragment
from iron_gql.slots import GQLFragment
from iron_gql.slots import SlotReader
from iron_gql.slots import SlotReaders
from iron_gql.websockets import async_graphql_ws_subscribe
from iron_gql.websockets import graphql_ws_subscribe
from iron_gql.websockets import ws_url

DEFAULT_QUERY_TIMEOUT = 10

# ASGI carries heterogeneous values, so `object` rather than `Any`: a fake that
# reads a scope key has to narrow it, and the type checker keeps that honest.
type ASGIScope = MutableMapping[str, object]
type ASGIEvent = MutableMapping[str, object]
type ASGIReceive = Callable[[], Awaitable[ASGIEvent]]
type ASGISend = Callable[[ASGIEvent], Awaitable[None]]
type _JSONValue = (
    bool | int | float | str | list[_JSONValue] | dict[str, _JSONValue] | None
)

ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


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


@dataclass(frozen=True, slots=True, kw_only=True, repr=False, eq=False)
class GQLOperation:
    _headers: tuple[tuple[str, str], ...] = ()

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    def with_headers(self, headers: dict[str, str]) -> Self:
        return replace(self, _headers=tuple(headers.items()))


# The base every generated template class derives from. Deliberately empty: a
# template is not executable and carries no state — it only exposes `bind()`,
# and each of its bindings is a `GQLBoundOperation` of its own. It exists so the
# generated `gql()`'s catch-all overload can name a type that covers a template,
# instead of widening to `object` and stripping every `gql(some_variable)` in
# the package of its typing.
class GQLTemplate:
    pass


# Generated definition class и typenames, на которых он достижим в slot.
# Одна entry на каждый fragment, читаемый в одном slot комбинации.
type SlotReaderSpec = tuple[FragmentDefinitionType, frozenset[str]]
# `exec_source` и readable-fragment spec для каждого slot. Template хранит одну
# такую entry на discovered combination в `_binding_specs`: document text и
# per-slot reader table.
type BoundSpec = tuple[str, dict[str, tuple[SlotReaderSpec, ...]]]


def _applied_value(
    fragment: GQLBindableFragment[pydantic.BaseModel, Any], name: str
) -> object:
    if name in fragment.fragment_args__:
        return fragment.fragment_args__[name]
    return OMITTED


def _canonical_request_json(value: object) -> str:
    # Сначала JSON model задаёт тот же public encoder, которым пользуется query
    # transport. В частности, object key `1` становится `"1"` до сортировки;
    # отдельная реализация такого преобразования дублировала бы wire contract
    # httpx2. Request только кодируется и не отправляется.
    encoded = httpx2.Request("POST", "http://request-shape.invalid", json=value).content
    adapter: pydantic.TypeAdapter[_JSONValue] = pydantic.TypeAdapter(_JSONValue)
    normalized = adapter.validate_json(encoded)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        sort_keys=True,
    )


def _request_shape(value: object) -> tuple[str, tuple[tuple[str, int], ...]]:
    # One applied value as the request carries it: the JSON `execute` would
    # send for it, plus every file riding along beside it -- which file, and
    # where in the value it sits.
    #
    # Compared instead of the objects themselves because `==` and the wire
    # disagree in both directions. It erases distinctions the request keeps --
    # `1 == True` and `1 == 1.0` are both true in Python, while a JSON scalar
    # receives `1`, `true` and `1.0`, three different values -- so two
    # applications that ask for different requests looked agreed, and the
    # second slot silently got the first one's value. And it keeps one the
    # wire erases: a `FileVar` serializes to `null` with the bytes riding in
    # the multipart body instead, so the JSON alone would call two different
    # uploads the same request.
    #
    # The whole `files` mapping, not the identities alone: what the multipart
    # body says about a file is a pair, and dropping the path answered "same
    # request" for one file offered at two different places -- `[file, None]`
    # and `[None, file]` have the same JSON (`[null, null]`) and the same
    # identity, and the merge then sent the first application's position for
    # both. Keeping only the paths loses the other half by the same argument.
    #
    # `serialize_variables` rather than a comparison of its own: it is what
    # `execute` sends, both halves of what it returns, and a second
    # implementation of "the same request" would be a copy to keep in step
    # with it.
    serialized, files = serialize_variables({"value": value})
    placements = tuple(sorted((path, id(file)) for path, file in files.items()))
    return _canonical_request_json(serialized), placements


def _same_request(value: object, other: object) -> bool:
    # `OMITTED` is no value and has no wire form: absence is what it asks
    # for, and only another absence agrees with it.
    if value is OMITTED or other is OMITTED:
        return value is other
    return _request_shape(value) == _request_shape(other)


def _merged_fragment_args(
    passed: Mapping[str, tuple[GQLBindableFragment[pydantic.BaseModel, Any], ...]],
) -> dict[str, object]:
    # Генерация доказывает, что разные direct factories владеют непересекающимися
    # именами variables. Динамическими остаются только повторные applications
    # одной factory: их конкретные значения появляются лишь при bind(), и до
    # слияния каждая application должна описывать один и тот же запрос.
    applications: dict[
        FragmentDefinitionType,
        list[tuple[str, GQLBindableFragment[pydantic.BaseModel, Any]]],
    ] = {}
    for slot, fragments in passed.items():
        for fragment in fragments:
            applications.setdefault(fragment.definition_type, []).append((
                slot,
                fragment,
            ))
    merged: dict[str, object] = {}
    for group in applications.values():
        _first_slot, first = _agreed_application(group[0][1].fragment_name__, group)
        merged.update(first.fragment_args__)
    return merged


def _agreed_application(
    fragment_name: str,
    group: list[tuple[str, GQLBindableFragment[pydantic.BaseModel, Any]]],
) -> tuple[str, GQLBindableFragment[pydantic.BaseModel, Any]]:
    # The one application every other one of the same fragment agrees with,
    # compared over the union of the names they wrote rather than over the keys
    # they happen to share: `with_args(width=100)` and `with_args(width=100,
    # size="LARGE")` overlap on every key the first one has, and merging by
    # presence alone therefore sent `size` for both spreads while the slot that
    # left it out had asked for the schema default. Whichever name they part
    # on is the one named in the diagnostic; the rest of the merge above then
    # reads a single agreed state per fragment.
    #
    # "Agree" is `_same_request`, not `==`: two applications agree exactly
    # when the request they ask for is the same one.
    first_entry, *rest = group
    first_slot, first = first_entry
    for other_slot, other in rest:
        for name in sorted(first.fragment_args__.keys() | other.fragment_args__.keys()):
            value = _applied_value(first, name)
            other_value = _applied_value(other, name)
            if _same_request(value, other_value):
                continue
            raise ValueError(
                _conflicting_value_message(
                    name=name,
                    first_owner=f"fragment '{fragment_name}' in slot '{first_slot}'",
                    second_owner=f"fragment '{fragment_name}' in slot '{other_slot}'",
                )
            )
    return first_entry


def _conflicting_value_message(
    *, name: str, first_owner: str, second_owner: str
) -> str:
    return (
        f"conflicting values for fragment variable ${name}: "
        f"{first_owner} and {second_owner} assign different request values. "
        f"The expanded document declares ${name} once, so "
        "every application of a fragment in one bind has to agree on it"
    )


@dataclass(frozen=True, slots=True, kw_only=True, repr=False, eq=False)
class GQLBoundOperation(GQLOperation):
    _exec_source: str
    _slot_readers: SlotReaders
    _fragment_args: Mapping[str, object]

    @property
    def exec_source(self) -> str:
        return self._exec_source

    @property
    def slot_readers(self) -> SlotReaders:
        return self._slot_readers

    @property
    def fragment_args(self) -> Mapping[str, object]:
        return self._fragment_args

    @classmethod
    def bound__(
        cls,
        spec: BoundSpec,
        passed: Mapping[str, tuple[GQLBindableFragment[pydantic.BaseModel, Any], ...]],
    ) -> Self:
        # Здесь static slot phantom встречается с общим runtime class всех
        # комбинаций. `render._bind_impl` вызывает метод сразу после lookup
        # `spec` в template binding specs. Applications дают только fragment variables;
        # readers создаются из generated definition classes внутри spec.
        # Поэтому definition и все applications одной factory разделяют
        # projection identity.
        exec_source, definitions_by_slot = spec
        return cls(
            _exec_source=exec_source,
            _slot_readers=MappingProxyType({
                slot: tuple(
                    SlotReader(
                        cast(
                            "Callable[[], GQLFragment[pydantic.BaseModel, Any]]",
                            definition_type,
                        )(),
                        typenames,
                    )
                    for definition_type, typenames in entries
                )
                for slot, entries in definitions_by_slot.items()
            }),
            _fragment_args=MappingProxyType(_merged_fragment_args(passed)),
        )


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
    slot_readers: SlotReaders | None,
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

    return result_type.model_validate(body.data, context=slot_readers)


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
        slot_readers: SlotReaders | None = None,
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
        return _parse_query_response(response, result_type, slot_readers)

    @asynccontextmanager
    async def subscribe[T: pydantic.BaseModel](
        self,
        result_type: type[T],
        query: str,
        *,
        variables: dict[str, Any],
        headers: dict[str, str],
        slot_readers: SlotReaders | None = None,
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
                    ws, result_type, query, serialized_vars, slot_readers
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
        slot_readers: SlotReaders | None = None,
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
        return _parse_query_response(response, result_type, slot_readers)

    @contextmanager
    def subscribe[T: pydantic.BaseModel](
        self,
        result_type: type[T],
        query: str,
        *,
        variables: dict[str, Any],
        headers: dict[str, str],
        slot_readers: SlotReaders | None = None,
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
                ws, result_type, query, serialized_vars, slot_readers
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
