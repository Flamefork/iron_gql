# iron_gql

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![main](https://github.com/Flamefork/iron_gql/actions/workflows/main.yml/badge.svg)](https://github.com/Flamefork/iron_gql/actions/workflows/main.yml)
[![PyPI - Version](https://img.shields.io/pypi/v/iron-gql)](https://pypi.org/project/iron-gql/)


`iron_gql` is a GraphQL code generator and runtime. It reads a schema SDL and your query documents, and it generates a typed Python client with Pydantic models. You can connect GraphQL APIs to services, CLIs, background jobs, and tests without hand-written boilerplate.

## Installation

```bash
pip install iron-gql            # runtime only (httpx2 + pydantic)
pip install iron-gql[codegen]   # + graphql-core for code generation
pip install iron-gql[testing]   # + uvicorn for the loopback test server
```

## Key Features
- **Query discovery.** `generate_gql_package` scans your codebase for calls of the form `<package>_gql("""...""")`. It validates each statement and writes a module with typed helpers.
- **Typed inputs and results.** The generated Pydantic models match every selection set, enum, and input object that the discovered queries reference.
- **Sync or async runtime.** Pick the mode per package with `mode="sync"` or `mode="async"`. The generated module targets `runtime.GQLClient` or `runtime.AsyncGQLClient`, both of which send requests through `httpx2`. One project can hold packages of both kinds.
- **ASGI in-process calls.** `AsyncGQLClient` accepts an ASGI `target_app` and then calls the app in-process without using the network. The sync client has no such transport: the ASGI transport of `httpx2` is async-only, and WSGI cannot carry websockets. Test synchronous packages against a real server on a loopback port, which [`iron_gql.testing`](#testing) starts for you.
- **Deterministic validation.** `graphql-core` (a codegen dependency) validates every statement against the schema. It rejects operations that share a name but have different bodies.

## Package Layout
- `runtime.py` contains `GQLClient` and `AsyncGQLClient`, the reusable `GQLOperation` base class, and the value serialization helpers.
- `codegen/generate.py` runs query discovery, validation, and module rendering.
- `codegen/parser.py` converts the GraphQL AST into typed helper structures for the renderer.
- `testing/` holds the test helpers: the client swap, the `graphql-transport-ws` server primitives, and the loopback server.

## Getting Started
1. **Describe your schema.** Write the schema in an SDL file (`schema.graphql`). Include the root types that you use (query, mutation, subscription).
2. **Write queries where you use them.** Import the helper that the generator will create. Wrap each GraphQL statement in a call to this helper:
   ```python
   from myapp.gql.client import client_gql

   get_user = client_gql(
       """
       query GetUser($id: ID!) {
           user(id: $id) {
               id
               name
           }
       }
       """
   )
   ```
   The generator derives the helper name (`client_gql`) from the package path that you ask it to build.
3. **Generate the client module.**
   ```python
   from pathlib import Path

   from iron_gql.codegen import generate_gql_package

   generate_gql_package(
       mode="async",
       schema_path=Path("schema.graphql"),
       src_path=Path("."),
       package_full_name="myapp.gql.client",
       base_url_import="myapp.config:GRAPHQL_URL",
       scalars={"ID": "builtins:str"},
       to_camel_fn_full_name="myapp.inflection:to_camel",
       to_snake_fn=my_project_to_snake,
       debug_path=Path("iron_gql/debug/myapp.gql.client"),
   )
   ```
   `mode` is required: pass `"async"` or `"sync"` for every package. Each package is self-contained, so one project can generate both.

   The call writes `myapp/gql/client.py`. The module contains:
   - a client singleton for the chosen mode,
   - Pydantic result and input models,
   - a query class per operation with typed `execute` methods,
   - overloads for the helper function, so editors can infer return types.
4. **Call your API.** An async package awaits `execute`:
   ```python
   async def fetch_user(user_id: str):
       query = get_user.with_headers({"Authorization": "Bearer token"})
       result = await query.execute(id=user_id)
       return result.user
   ```
   A sync package calls it directly:
   ```python
   def fetch_user(user_id: str):
       query = get_user.with_headers({"Authorization": "Bearer token"})
       result = query.execute(id=user_id)
       return result.user
   ```

## Custom Scalars

The generator maps GraphQL scalars to Python types in two layers.

It maps **built-in scalars** automatically:

| GraphQL | Python |
|---------|--------|
| `String`, `Int`, `Float`, `Boolean` | `str`, `int`, `float`, `bool` |
| `Date` | `datetime.date` |
| `DateTime` | `datetime.datetime` |
| `JSON` | `object` |
| `Upload` | `iron_gql.FileVar` |

You configure **custom scalars** with the `scalars` parameter, in `"module:type"` format:

```python
generate_gql_package(
    ...,
    scalars={
        "ID": "builtins:str",
        "Money": "decimal:Decimal",
        "ULID": "ulid:ULID",
    },
)
```

Custom scalar types must be Pydantic-compatible. That is, Pydantic must know how to parse the type from JSON (deserialization) and how to serialize it to JSON. Standard library types (`datetime`, `Decimal`, `UUID`, `Enum`) are compatible by default. Any type that implements `__get_pydantic_core_schema__` is also compatible. The generator maps unknown scalars to `object` and writes a warning to the log.

## Fragment Slots

Shared infrastructure code often owns a GraphQL operation, but it does not know which fields each caller needs on some field of that operation. A fragment slot lets each caller supply its own fragment for that field at call time. The operation does not have to name the fragments of its consumers in advance.

Mark a field with `@slot` in a query, mutation, or subscription. Do not use `@slot` inside a fragment definition. Give the field a static selection that selects `__typename` at the top level of its own selection set. This `__typename` must be unaliased and must have no directives. It must not come through an inline fragment or a fragment spread. The slot field itself cannot have `@skip` or `@include`: the operation always requests the slot field. If a caller wants no fragment data, it passes an empty list.

```python
get_post_attachment = api_gql("""
    query GetPostAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
""")
```

A statement that holds exactly one fragment definition becomes a typed **handle** when some slot in the package can accept the fragment. A slot can accept a fragment when the fragment is spread-compatible with the type of the slot field. You can still spread the same fragment by name into other operations. For two kinds of statement, the helper returns a plain `runtime.GQLOperation`:

- a statement with one fragment definition that no slot accepts,
- a statement with several fragment definitions and no operation.

The fragments of these statements continue to work as building blocks for name spreads. They carry none of the obligations of a handle: self-containedness, a `__typename` of its own on polymorphic selections, and a non-empty selection. For a statement that contains an operation, the helper returns the class of that operation, as always.

```python
IMAGE_URL = api_gql("""
    fragment ImageUrl on ImageAttachment {
        url
    }
""")
```

Pass a handle, or a sequence of handles, into `execute`. The keyword argument is the snake_case form of the name of the slot field, or of its alias. For example, `mainAttachment @slot` becomes `main_attachment=`. Then read the typed model of each fragment from the slot node with `handle.read(node)`:

```python
result = await get_post_attachment.execute(id="p-1", attachment=IMAGE_URL)
if result.post is not None:
    image = IMAGE_URL.read(result.post.attachment)
    if image is not None:
        print(image.url)
```

`read` returns `None` in exactly two situations:

- The node is `None` because the server sent `null`.
- The runtime type of the node is outside the fragment's own selection.

If you read with a handle that you never passed to that slot, `read` raises an error. It does not return `None`, because a wiring bug must not look like a legitimate mismatch.

Fragments are isolated from each other. Each fragment reads exactly its own selection. It never receives the fields that the fragment of another caller selected.

You can reach slot data only through `read`. The data is not part of the fields of the result model, so `model_dump()` does not include it. A dumped result does not round-trip. To validate the dump again, you must supply a fragments context. Without one, validation fails with an error. With or without one, the data of the fragments is gone.

For each slot field type, the generator writes one compatibility base class, named `{FieldType}Fragment`. With this class, shared code can be generic over any fragment that is compatible with that field. The shared code does not have to know the concrete shape of the fragment:

```python
async def read_attachment[T: pydantic.BaseModel](
    post_id: str, fragment: AttachmentFragment[T]
) -> T | None:
    result = await get_post_attachment.execute(id=post_id, attachment=fragment)
    if result.post is None:
        return None
    return fragment.read(result.post.attachment)
```

A fragment that is not spread-compatible with the slot causes a type error. The type checker finds the mismatch before you ship your code.

The runtime validates every fragment that you pass into a slot at the response boundary. For queries and mutations, validation occurs inside `execute`. For subscriptions, validation occurs on each received message. Malformed data causes an immediate error. The malformed data never surfaces later from `read`.

Three rules apply:
- A handle must be self-contained. It cannot spread other fragments, and it cannot reference variables (`$name`). The handle travels to the server as its own text, next to an operation that declares nothing for it. Fragments that no slot accepts are free of both rules. They can spread other fragments by name, and they take their variables from the operations that spread them.
- An operation that declares a compatible slot cannot define or spread any fragment name that a handle ships. This covers the handle's own name and its transitive dependencies. The generator rejects the combination as soon as the handle exists, whether or not anyone passes the handle. To remove the conflict, rename one of the two. Operations without a compatible slot are outside this rule. The same fragment can work in both roles across different operations.
- The slot keyword argument in `execute` is mandatory, and there is no default. To send no fragments, pass an empty list explicitly.

## Customization Hooks
- **Naming conventions.** Supply `to_camel_fn_full_name` (a module:path string) and a `to_snake_fn` callable. These functions align the casing with your own `alias_generator`.
- **Endpoint configuration.** The generator writes `base_url_import` verbatim into the generated module. Set it to a global string, a configuration object, or a helper that returns the GraphQL endpoint.

## Runtime Highlights
- `AsyncGQLClient` accepts an ASGI `target_app`. You can use the same runtime for production HTTP calls and for in-process ASGI execution.
- `GQLClient.subscribe` returns a plain context manager over a blocking generator, so a synchronous consumer reads subscription messages with `for`.
- `GQLOperation.with_headers` clones the operation object. The original does not change, so each call can have its own headers.
- `Upload` scalars map to `iron_gql.FileVar`. When variables contain `FileVar` instances, the client automatically sends a multipart upload (see the [GraphQL multipart request spec](https://github.com/jaydenseric/graphql-multipart-request-spec)).
- `serialize_var` converts variables to JSON-compatible structures with the Pydantic `TypeAdapter`. It supports custom scalar types, nested models, dicts, and lists.

## Example

The [`example/`](example/) directory contains a complete working setup. It has a GraphQL schema with queries, mutations, enums, interfaces, unions, and fragments. It also has the generation script and sample query definitions. See [`example/generate.py`](example/generate.py) for the codegen calls of both modes, [`example/main.py`](example/main.py) for async query usage, and [`example/main_sync.py`](example/main_sync.py) for the synchronous form.

## Testing

`iron_gql.testing` holds the helpers that a service needs to test its own use of a generated package.

Which transport a test needs follows from the client:

| client | queries and mutations | subscriptions |
|---|---|---|
| `AsyncGQLClient` | `target_app`, no socket | `target_app`, no socket |
| `GQLClient` | server on a loopback port | server on a loopback port |

`AsyncGQLClient` takes an ASGI `target_app` and calls it in process, websockets included, so an async package never needs a socket. `GQLClient` has no such transport: the ASGI transport of `httpx2` is async-only, and WSGI cannot carry websockets. A synchronous package is therefore tested against a real server, which `live_asgi_server` starts for you.

Only `live_asgi_server` has a dependency of its own — `uvicorn`, from `pip install iron-gql[testing]`. Everything else here needs nothing beyond the runtime, so a project that only replaces clients installs plain `iron-gql`.

You supply the fake yourself. The library takes no position on how you answer a query: run your own schema library, return canned JSON, or serve the app you are testing.

### Replace the client

`use_async_client` and `use_sync_client` bind your own client into a generated package. On exit each one restores the previous client and closes the client that you passed in:

```python
from iron_gql.runtime import AsyncGQLClient
from iron_gql.testing import use_async_client
from myapp.gql import api

async def test_get_user():
    client = AsyncGQLClient(base_url="http://testserver", target_app=my_asgi_app)
    async with use_async_client(api, client):
        result = await get_user.execute(id="1")
        assert result.user.name == "Alice"
```

The generated query classes resolve the client by module attribute name at call time, so this replacement is sufficient. The helpers derive that name from the name of the module, exactly as the generator derives it. For the package `myapp.gql.api`, the attribute is `API_CLIENT`.

### Serve an app on a loopback port

`live_asgi_server` serves an ASGI app with `uvicorn` on a port that the operating system picks, and it yields the URL of that server:

```python
from iron_gql.runtime import GQLClient
from iron_gql.testing import use_sync_client
from iron_gql.testing.server import live_asgi_server

def test_get_user_sync():
    with (
        live_asgi_server(my_asgi_app) as base_url,
        use_sync_client(api, GQLClient(base_url=base_url)),
    ):
        result = get_user.execute(id="1")
        assert result.user.name == "Alice"
```

The URL ends with `/graphql`. Pass `path="/other"` for a different one. The path is only a part of the URL: a fake app usually answers on every path, so the helper does not route.

### Script a subscription fake

Subscriptions speak `graphql-transport-ws`, and a fake has to hold up the server end of it. `accept_graphql_ws` performs the handshake and hands you the connection; each step then asserts that the client kept to the protocol, and reports what arrived instead when it did not.

```python
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.testing import accept_graphql_ws

async def events_app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
    connection = await accept_graphql_ws(scope, receive, send)
    subscription = await connection.ack()
    assert subscription.payload["variables"] == {"channel": "test"}

    await subscription.next({"events": {"id": "1", "message": "hello"}})
    await subscription.complete()
    await connection.drain()
```

`accept_graphql_ws` accepts the socket, echoes the subprotocol when the client offered it, and consumes `connection_init`. On the connection you then call:

- `ack()` — sends `connection_ack` and waits for the client's `subscribe`. The subscription it returns exposes the received message as `payload` and stamps its `id` on everything it sends.
- `send_message(message)` and `expect_pong()` — for `ping` and other connection-level traffic.
- `close(code, reason)` — closes the socket, for testing how the client reacts.
- `drain()` — waits for the client to hang up. Return before that and you tear the connection down under it.

On the subscription: `next(data)`, `error(errors)`, `complete()`, and `send_message(message)` for anything else.

Control flow stays in your fake, so state lives in ordinary Python around these calls — a connection counter, a drop on the N-th connect, a failure of the first few messages.

### Keep the generated code current

`generate_gql_package` returns `True` when it wrote a change. Committing generated modules and asserting the generator has nothing left to write catches a schema or a query that moved ahead of the committed code:

```python
def test_generated_package_is_current():
    assert generate_gql_package(...) is False
```

## Validation and Troubleshooting
- Error messages show the file and the line of the statement that caused the error.
- Operations that share a name must have identical bodies. To remove the conflict, rename the operations or merge them.
- The generated helper raises `LookupError` for a statement that it does not know. After you add or edit a statement, regenerate the package.
