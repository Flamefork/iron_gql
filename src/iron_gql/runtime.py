import copy
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any
from typing import Self

import gql
import graphql
import httpx
import pydantic
from gql.transport import httpx as gql_httpx

DEFAULT_QUERY_TIMEOUT = 10

_ASGIApp = Callable[
    [
        MutableMapping[str, Any],
        Callable[[], Awaitable[MutableMapping[str, Any]]],
        Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


class GQLQuery:
    def __init__(self):
        self.headers: dict[str, str] = {}
        self.upload_files: bool = False

    def __deepcopy__(self, memo: dict[int, Any] | None):
        q = self.__class__()
        q.headers = copy.deepcopy(self.headers, memo)
        q.upload_files = self.upload_files
        return q

    def with_headers(self, headers: dict[str, str]) -> Self:
        q = copy.deepcopy(self)
        q.headers = headers
        return q

    def with_file_uploads(self) -> Self:
        q = copy.deepcopy(self)
        q.upload_files = True
        return q


class GQLClient:
    def __init__(
        self,
        *,
        base_url: str,
        target_app: _ASGIApp | None = None,
        schema: str | Path,
        headers: dict[str, str] | None = None,
        query_timeout: int = DEFAULT_QUERY_TIMEOUT,
    ):
        self.base_url = base_url
        self.target_app = target_app
        self.headers = headers
        self.query_timeout = query_timeout

        if isinstance(schema, Path):
            with schema.open(encoding="utf-8") as f:
                schema = f.read()
        self.schema = graphql.build_ast_schema(graphql.parse(schema))

    async def query[T: pydantic.BaseModel](
        self,
        result_type: type[T],
        document: graphql.DocumentNode,
        variable_values: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        upload_files: bool = False,
    ) -> T:
        """Execute a GraphQL query and return validated result.

        Args:
            result_type: Pydantic model class to validate the response against
            document: Parsed GraphQL document (query/mutation)
            variable_values: Optional dictionary of GraphQL variables
            headers: Optional HTTP headers to merge with client defaults
            upload_files: Whether to enable file upload support

        Returns:
            Validated instance of result_type with the query response data
        """
        httpx_transport = (
            httpx.ASGITransport(app=self.target_app) if self.target_app else None
        )
        gql_transport = gql_httpx.HTTPXAsyncTransport(
            url=self.base_url,
            transport=httpx_transport,
            headers=(self.headers or {}) | (headers or {}),
            timeout=self.query_timeout,
        )
        client = gql.Client(
            transport=gql_transport,
            schema=self.schema,
            execute_timeout=self.query_timeout,
            serialize_variables=True,
            parse_results=True,
        )
        async with client as session:
            result = await session.execute(
                document,
                variable_values,
                upload_files=upload_files,
            )
            return result_type.model_validate(result)


def serialize_var(value: Any) -> Any:
    match value:
        case list() | tuple():
            return [serialize_var(v) for v in value]
        case dict():
            return {k: serialize_var(v) for k, v in value.items()}
        case pydantic.BaseModel():
            return value.model_dump(mode="json", by_alias=True)
        case _:
            return value
