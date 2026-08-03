# iron_gql

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![main](https://github.com/Flamefork/iron_gql/actions/workflows/main.yml/badge.svg)](https://github.com/Flamefork/iron_gql/actions/workflows/main.yml)
[![PyPI - Version](https://img.shields.io/pypi/v/iron-gql)](https://pypi.org/project/iron-gql/)


`iron_gql` is a lightweight GraphQL code generator and runtime that turns schema SDL and real query documents into typed Python clients powered by Pydantic models. Use it to wire GraphQL APIs into services, CLIs, background jobs, or tests without hand-writing boilerplate.

## Installation

```bash
pip install iron-gql            # runtime only (httpx + pydantic)
pip install iron-gql[codegen]   # + graphql-core for code generation
```

## Key Features
- **Query discovery.** `generate_gql_package` scans your codebase for calls that look like `<package>_gql("""...""")`, validates each statement, and emits a module with strongly typed helpers.
- **Typed inputs and results.** Generated Pydantic models mirror every selection set, enum, and input object referenced by the discovered queries.
- **Async runtime.** `runtime.GQLClient` speaks to GraphQL endpoints over `httpx` and can shortcut network hops when pointed at an ASGI app.
- **Deterministic validation.** `graphql-core` (codegen dependency) enforces schema compatibility and rejects duplicate operation names with incompatible bodies.

## Package Layout
- `runtime.py` – provides the async `GQLClient`, the reusable `GQLOperation` base class, and value serialization helpers.
- `codegen/generate.py` – orchestrates query discovery, validation, and module rendering.
- `codegen/parser.py` – converts GraphQL AST into typed helper structures consumed by the renderer.

## Getting Started
1. **Describe your schema.** Point `generate_gql_package` at an SDL file (`schema.graphql`). Include whichever root types you rely on (query, mutation, subscription).
2. **Author queries where they live.** Import the future helper and wrap your GraphQL statement:
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
   The generator infers the helper name (`client_gql`) from the package path you ask it to build.
3. **Generate the client module.**
   ```python
   from pathlib import Path

   from iron_gql.codegen import generate_gql_package

   generate_gql_package(
       schema_path=Path("schema.graphql"),
       package_full_name="myapp.gql.client",
       base_url_import="myapp.config:GRAPHQL_URL",
       scalars={"ID": "builtins:str"},
       to_camel_fn_full_name="myapp.inflection:to_camel",
       to_snake_fn=my_project_to_snake,
       debug_path=Path("iron_gql/debug/myapp.gql.client"),
       src_path=Path("."),
   )
   ```
   The call writes `myapp/gql/client.py` containing:
   - an async client singleton,
   - Pydantic result and input models,
   - a query class per operation with typed `execute` methods,
   - overloads for the helper function so editors can infer return types.
4. **Call your API.**
   ```python
   async def fetch_user(user_id: str):
       query = get_user.with_headers({"Authorization": "Bearer token"})
       result = await query.execute(id=user_id)
       return result.user
   ```

## Custom Scalars

The generator maps GraphQL scalars to Python types in two layers:

**Built-in scalars** are mapped automatically:

| GraphQL | Python |
|---------|--------|
| `String`, `Int`, `Float`, `Boolean` | `str`, `int`, `float`, `bool` |
| `Date` | `datetime.date` |
| `DateTime` | `datetime.datetime` |
| `JSON` | `object` |
| `Upload` | `iron_gql.FileVar` |

**Custom scalars** are configured via the `scalars` parameter in `"module:type"` format:

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

Custom scalar types must be Pydantic-compatible — i.e. Pydantic should know how to parse them from JSON (deserialization) and serialize them to JSON. This works out of the box for standard library types (`datetime`, `Decimal`, `UUID`, `Enum`) and for any type that implements `__get_pydantic_core_schema__`. Unknown scalars fall back to `object` with a log warning.

## Fragment Slots

Shared infrastructure code often owns a GraphQL operation without knowing which fields its callers need on one of the operation's fields. Fragment slots let each caller supply its own fragment for that field at call time, instead of the operation naming every consumer's fragment up front.

Mark a field with `@slot` in a query, mutation, or subscription (not inside a fragment definition), giving it a static selection that selects `__typename` at the top level of the field's own selection set: unaliased, with no directives on it, and not through an inline fragment or a fragment spread. The slot field itself cannot carry `@skip`/`@include` — a slot is always requested, and a caller that wants no fragment data passes an empty list.

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

A statement holding exactly one fragment definition becomes a typed **handle** when some slot in the package can accept it — that is, when the fragment is spread-compatible with a slot field's type. The same fragment can still be spread by name into other operations as before. A single-fragment statement no slot accepts, and a statement bundling several fragment definitions without an operation, keep returning a plain `runtime.GQLOperation`: their fragments live on as name-spread building blocks and owe none of a handle's obligations (self-containedness, own `__typename` on polymorphic selections, non-empty selection). A statement containing an operation returns that operation's class exactly as it always has:

```python
IMAGE_URL = api_gql("""
    fragment ImageUrl on ImageAttachment {
        url
    }
""")
```

Pass a handle, or a sequence of handles, into `execute` using the snake_case form of the slot field's name (or alias) as the keyword argument — `mainAttachment @slot` becomes `main_attachment=` — then read each fragment's own typed model back out of the slot node with `handle.read(node)`:

```python
result = await get_post_attachment.execute(id="p-1", attachment=IMAGE_URL)
if result.post is not None:
    image = IMAGE_URL.read(result.post.attachment)
    if image is not None:
        print(image.url)
```

`read` returns `None` in exactly two situations: the node itself is `None` because the server sent `null`, or the node's runtime type is outside the fragment's own selection. Reading with a handle that was never passed to that slot raises instead of returning `None` — a wiring bug must not look like a legitimate mismatch. Fragments are isolated from each other: each one reads back exactly its own selection, never the fields another caller's fragment asked for. Slot data is reachable only through `read`: it is not part of the result model's fields, so `model_dump()` does not include it — and a dumped result does not round-trip: re-validating it demands a fragments context again (without one it fails loudly), and the fragments' data is gone either way. The generator emits one compatibility base per slot field type, named `{FieldType}Fragment`, so shared code can be generic over any fragment compatible with that field, without knowing its concrete shape:

```python
async def read_attachment[T: pydantic.BaseModel](
    post_id: str, fragment: AttachmentFragment[T]
) -> T | None:
    result = await get_post_attachment.execute(id=post_id, attachment=fragment)
    if result.post is None:
        return None
    return fragment.read(result.post.attachment)
```

Passing a fragment that isn't spread-compatible with the slot is a type error, so mismatches are caught before your code ships.

Validation of every fragment passed into a slot happens eagerly — for queries and mutations inside `execute`, for subscriptions on every received message — so malformed data raises at the response boundary and never silently surfaces later from `read`.

Three things to keep in mind:
- A handle must be self-contained: it cannot spread other fragments and cannot reference variables (`$name`) — it travels to the server as its own text alone, next to an operation that declares nothing on its behalf. Fragments no slot accepts are untouched by both rules: they keep composing through name spreads and taking their variables from the operations that spread them.
- An operation that declares a compatible slot cannot itself define or spread any fragment name a handle ships — the handle's own name or one of its transitive dependencies. The generator rejects the combination as soon as the handle exists, whether or not anyone passes it; rename one of the two. Operations without a compatible slot are outside the rule, and the same fragment works in both roles across different operations.
- The slot keyword argument in `execute` is mandatory — there is no default, so sending no fragments means passing an empty list explicitly.

## Customization Hooks
- **Naming conventions.** Supply `to_camel_fn_full_name` (module:path string) and a `to_snake_fn` callable to align casing with your own `alias_generator`.
- **Endpoint configuration.** `base_url_import` is written verbatim into the generated module; point it at a global string, config object, or helper that returns the GraphQL endpoint.

## Runtime Highlights
- `GQLClient` accepts ASGI `target_app` so you can reuse the runtime for production HTTP calls or in-process ASGI execution.
- `GQLOperation.with_headers` clones the operation object, making per-call customization trivial.
- `Upload` scalars map to `iron_gql.FileVar`; multipart upload per the [GraphQL multipart request spec](https://github.com/jaydenseric/graphql-multipart-request-spec) is triggered automatically when variables contain `FileVar` instances.
- `serialize_var` converts variables to JSON-friendly structures via Pydantic's `TypeAdapter`, supporting custom scalar types alongside nested models, dicts, and lists.

## Example

The [`example/`](example/) directory contains a complete working setup: a GraphQL schema with queries, mutations, enums, interfaces, unions, and fragments, plus the generation script and sample query definitions. See [`example/generate.py`](example/generate.py) for the codegen call and [`example/main.py`](example/main.py) for query usage.

## Testing

Override the generated client via `monkeypatch` (or any other module attribute patching) to point queries at a test server or an ASGI app:

```python
from iron_gql import runtime
from myapp.gql import api

async def test_get_user(monkeypatch):
    test_client = runtime.GQLClient(
        base_url="http://testserver",
        target_app=my_asgi_app,
    )
    monkeypatch.setattr(api, "API_CLIENT", test_client)

    result = await get_user.execute(id="1")
    assert result.user.name == "Alice"
```

The generated query classes resolve the client by module attribute name at call time, so replacing it is sufficient. The attribute is always named `{PACKAGE}_CLIENT` — for a package `myapp.gql.api` it is `API_CLIENT`.

## Validation and Troubleshooting
- Errors identify the file and line where the problematic statement lives.
- Duplicate operation names must share identical bodies; rename or consolidate to resolve the conflict.
- Calling the generated helper with a statement it does not know raises `LookupError`: after adding or editing a statement, regenerate the package.
