"""The responses a GraphQL server can send over HTTP, crossed on their axes.

Status, media type, the `data` entry and the `errors` entry vary on their own,
so they are crossed rather than listed: a list written case by case covered
the cells its rows thought of -- a GraphQL body with no usable `errors` only
under 500, never under 400.

Every axis value says what it *is* by the specs, not what the client makes of
it: a media type is the GraphQL one or not (GraphQL over HTTP), an `errors`
entry is a well-formed list of GraphQL errors, absent, or malformed (GraphQL,
"Errors": a non-empty list of objects, each with a string `message`). What the
client must do with a case is then read off the table of the ticket that
decided it, in `expected`, from those labels alone.
"""

import itertools
import json
from dataclasses import dataclass
from typing import Literal

# The class of a status, as HTTP defines it.
type StatusClass = Literal["success", "redirect", "error"]


@dataclass(frozen=True, kw_only=True)
class Status:
    name: str
    code: int
    status_class: StatusClass


STATUSES: tuple[Status, ...] = (
    Status(name="200", code=200, status_class="success"),
    # A 2xx no server sends for GraphQL: a success all the same.
    Status(name="294", code=294, status_class="success"),
    Status(name="307", code=307, status_class="redirect"),
    Status(name="400", code=400, status_class="error"),
    Status(name="401", code=401, status_class="error"),
    Status(name="500", code=500, status_class="error"),
)


@dataclass(frozen=True, kw_only=True)
class MediaType:
    name: str
    header: str | None
    # `application/graphql-response+json`, compared without parameters and
    # regardless of case.
    graphql: bool


MEDIA_TYPES: tuple[MediaType, ...] = (
    MediaType(name="graphql", header="application/graphql-response+json", graphql=True),
    MediaType(
        name="graphql-params-case",
        header="Application/GraphQL-Response+JSON ; charset=utf-8",
        graphql=True,
    ),
    MediaType(name="json", header="application/json", graphql=False),
    # The `+json` suffix alone does not make a GraphQL response.
    MediaType(name="problem-json", header="application/problem+json", graphql=False),
    MediaType(name="none", header=None, graphql=False),
)

type ErrorsKind = Literal["errors", "none", "malformed"]


@dataclass(frozen=True, kw_only=True)
class ErrorsForm:
    name: str
    # The JSON value of the `errors` entry; `None` leaves the entry out.
    value: object
    kind: ErrorsKind


_LOGIN_REQUIRED = {
    "message": "Login required",
    "extensions": {"code": "UNAUTHENTICATED"},
}

ERRORS_FORMS: tuple[ErrorsForm, ...] = (
    ErrorsForm(name="absent", value=None, kind="none"),
    ErrorsForm(name="empty", value=[], kind="none"),
    ErrorsForm(name="one-error", value=[_LOGIN_REQUIRED], kind="errors"),
    ErrorsForm(
        name="two-errors",
        value=[{"message": "a", "path": ["ping"]}, {"message": "b"}],
        kind="errors",
    ),
    ErrorsForm(name="message-null", value=[{"message": None}], kind="malformed"),
    ErrorsForm(
        name="no-message", value=[{"extensions": {"code": "X"}}], kind="malformed"
    ),
    ErrorsForm(name="error-a-string", value=["Login required"], kind="malformed"),
    ErrorsForm(name="errors-a-string", value="Login required", kind="malformed"),
)

type DataKind = Literal["data", "null", "absent"]

# The `data` entry: the operation's result, `null`, or left out.
DATA_FORMS: tuple[DataKind, ...] = ("data", "null", "absent")

# What the result of both operations holds when `data` is there: the query
# reads `ping`, and a subscription is never handed a single response as data.
DATA: dict[str, object] = {"ping": "pong"}


@dataclass(frozen=True, kw_only=True)
class Body:
    name: str
    content: bytes
    data: DataKind | None
    # `None` for a body that is not a JSON object at all.
    errors: ErrorsForm | None


def _json_body(data: DataKind, errors: ErrorsForm) -> Body:
    document: dict[str, object] = {}
    match data:
        case "data":
            document["data"] = DATA
        case "null":
            document["data"] = None
        case "absent":
            pass
    if errors.value is not None:
        document["errors"] = errors.value
    return Body(
        name=f"data-{data}-errors-{errors.name}",
        content=json.dumps(document).encode(),
        data=data,
        errors=errors,
    )


# A subscription message's payload carrying this `errors` entry, beside the
# `data` the message holds.
def with_errors(payload: dict[str, object], form: ErrorsForm) -> dict[str, object]:
    return payload if form.value is None else {**payload, "errors": form.value}


BODIES: tuple[Body, ...] = (
    *itertools.starmap(_json_body, itertools.product(DATA_FORMS, ERRORS_FORMS)),
    Body(name="not-json", content=b"Internal Server Error", data=None, errors=None),
    Body(name="not-an-object", content=b"[1, 2]", data=None, errors=None),
)


@dataclass(frozen=True, kw_only=True)
class ResponseCase:
    status: Status
    media_type: MediaType
    body: Body

    @property
    def id(self) -> str:
        return f"{self.status.name}-{self.media_type.name}-{self.body.name}"


def build_corpus() -> list[ResponseCase]:
    return [
        ResponseCase(status=status, media_type=media_type, body=body)
        for status, media_type, body in itertools.product(STATUSES, MEDIA_TYPES, BODIES)
    ]


# What the client does with a response, as the caller sees it.
type Outcome = Literal[
    # A query returns its data.
    "data",
    # `GraphQLResponseError` carrying the body's `errors` as they came.
    "errors",
    # `GraphQLResponseError`: the body is not a GraphQL response.
    "malformed",
    # `GraphQLResponseError`: a query's response holds no data.
    "no-data",
    # `GraphQLResponseError`: a subscription got a single response, no stream.
    "not-a-stream",
    # `httpx2.HTTPStatusError` for a redirect, which is not followed.
    "redirect",
    # `httpx2.HTTPStatusError` with the response, its status and body inside.
    "status",
]


# The table of the ticket that decided the rule, for a query's response and
# for a subscription's response in place of a stream. `None` where the
# behaviour is not decided yet: a 2xx body that is not JSON (open ticket
# `graphql-over-http-client/02`).
def expected(case: ResponseCase, *, subscription: bool) -> Outcome | None:
    errors = case.body.errors
    if case.status.status_class == "redirect":
        return "redirect"
    if case.status.status_class == "error":
        explained = (
            case.media_type.graphql and errors is not None and errors.kind == "errors"
        )
        return "errors" if explained else "status"
    # 2xx: read the same whatever the media type.
    if case.body.name == "not-json":
        return None
    if errors is None or errors.kind == "malformed":
        return "malformed"
    if errors.kind == "errors":
        return "errors"
    if subscription:
        return "not-a-stream"
    return "data" if case.body.data == "data" else "no-data"


# The `Accept` a request asks with. The caller can set one on the client and
# on the call; each side either leaves it out or sets a value -- empty too,
# and under any spelling of the header's name.
@dataclass(frozen=True, kw_only=True)
class AcceptSetting:
    name: str
    headers: tuple[tuple[str, str], ...]
    # The value set; `None` when the headers set none.
    accept: str | None


CLIENT_ACCEPTS: tuple[AcceptSetting, ...] = (
    AcceptSetting(name="client-none", headers=(), accept=None),
    AcceptSetting(
        name="client-json",
        headers=(("Accept", "application/json"),),
        accept="application/json",
    ),
    AcceptSetting(name="client-upper-any", headers=(("ACCEPT", "*/*"),), accept="*/*"),
    AcceptSetting(name="client-empty", headers=(("Accept", ""),), accept=""),
)

CALL_ACCEPTS: tuple[AcceptSetting, ...] = (
    AcceptSetting(name="call-none", headers=(), accept=None),
    AcceptSetting(
        name="call-lower-json",
        headers=(("accept", "application/json"),),
        accept="application/json",
    ),
    AcceptSetting(name="call-empty", headers=(("Accept", ""),), accept=""),
)


@dataclass(frozen=True, kw_only=True)
class AcceptCase:
    client: AcceptSetting
    call: AcceptSetting

    @property
    def id(self) -> str:
        return f"{self.client.name}-{self.call.name}"


def build_accept_corpus() -> list[AcceptCase]:
    return [
        AcceptCase(client=client, call=call)
        for client, call in itertools.product(CLIENT_ACCEPTS, CALL_ACCEPTS)
    ]


# GraphQL over HTTP asks a client that does not know its server's media type
# for the GraphQL one first and JSON after it; a subscription asks for exactly
# `text/event-stream`, the only value the reference graphql-sse server opens a
# stream for.
QUERY_ACCEPT = "application/graphql-response+json, application/json;q=0.9"
STREAM_ACCEPT = "text/event-stream"


# The `Accept` the request must arrive with: the caller's as it is -- the
# call's over the client's -- and the client's own for none.
def expected_accept(case: AcceptCase, *, subscription: bool) -> str:
    if case.call.accept is not None:
        return case.call.accept
    if case.client.accept is not None:
        return case.client.accept
    return STREAM_ACCEPT if subscription else QUERY_ACCEPT
