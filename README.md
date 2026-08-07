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

Shared infrastructure code often owns a GraphQL operation, but it does not know in advance which fields each caller needs on some field of that operation. A fragment slot lets each caller supply its own fragment for that field. The generator resolves every slot at codegen time, so each caller ends up with its own fully static operation — no query text is assembled at call time.

Mark a field with `@slot` in a query, mutation, or subscription. Do not use `@slot` inside a fragment definition, and do not nest a slot inside another slot's own selection. Give the field a static selection that selects `__typename` at the top level of its own selection set. This `__typename` must be unaliased and must have no directives. It must not come through an inline fragment or a fragment spread. The slot field itself cannot have `@skip` or `@include`.

An operation that contains a `@slot` is a **template**. It has no `execute` of its own, only `bind`. Write it wherever you write any other statement — a module-level name is how you share one template between modules, not a condition of using it:

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

A statement that holds exactly one fragment definition becomes a typed **fragment handle** once some `bind()` call reaches it, directly or through another fragment's own spread. A fragment that no `bind()` reaches keeps its plain meaning: spread it by name into other operations, as you always could.

```python
image_url = api_gql("""
    fragment ImageUrl on ImageAttachment {
        url
    }
""")

link_url = api_gql("""
    fragment LinkUrl on LinkAttachment {
        href
    }
""")
```

### Binding fragments to a template

Link a template to the fragments it should carry with `bind()`:

```python
get_post_attachment_image = get_post_attachment.bind(attachment=image_url)
get_post_attachment_link = get_post_attachment.bind(attachment=link_url)
```

The keyword argument is the snake_case form of the slot field's name, or of its alias if it has one.

A bind is an ordinary expression. It can sit in a function body next to the code that executes it, and the template and fragments it names can be local there too — or written straight into the call:

```python
async def show_attachment(post_id: str) -> None:
    bound = get_post_attachment.bind(attachment=api_gql("""
        fragment ImageAlt on ImageAttachment {
            alt
        }
    """))
    result = await bound.execute(id=post_id)
```

Each name a bind reads — the template and every fragment — has to be resolvable where it is written: a name bound exactly once, in the scope the call sees, by a single `api_gql("...")` statement. That is what a module-level name, a local one, a walrus target and a directly imported one all have in common. A name bound twice — two assignments, two imports, or a mix — is not, and neither is one reached through an attribute chain (`infra.template`, `Registry.template`).

A slot can also take a list of fragments. Their covered runtime types may overlap — each fragment reads its own slice of the payload independently. What the generator still rejects is a field-merge conflict in the expanded operation (the same response key selected with different arguments), reported by standard GraphQL validation. A fragment bound alone to a slot in one bind can also appear inside a list bind of that same slot in another bind — a registry bind and a single-reader bind of the same slot coexist, each with its own generated class:

```python
image_caption = api_gql("""
    fragment ImageCaption on ImageAttachment {
        caption
    }
""")

link_summary = api_gql("""
    fragment LinkSummary on LinkAttachment {
        href
    }
""")

get_post_attachment_any = get_post_attachment.bind(
    attachment=[image_caption, link_summary]
)
```

Slots you do not name in `bind()` stay unfilled: the operation still requests their static `__typename` selection, but no fragment data comes back for them. Omitting a slot and passing it an explicit empty list (`attachment=[]`) mean the same thing. The same fragment can be bound into any number of templates, and a template can have any number of binds.

A binding **is** its combination — the template plus the fragments in each slot — and the generated class is named after that combination, never after the variable a call site assigns it to:

| bind | generated class |
|------|-----------------|
| `get_post_attachment.bind(attachment=image_url)` | `GetPostAttachmentWithAttachmentImageUrl` |
| `get_post_attachment.bind(attachment=[image_caption, link_summary])` | `GetPostAttachmentWithAttachmentImageCaptionLinkSummary` |
| `get_post_attachment.bind()` | `GetPostAttachmentWithNothing` |

One `With{Slot}{Fragments…}` group per filled slot, slots and fragments in sorted order, unfilled slots left out. Two call sites that write the same combination therefore mean one class, however each spells it and wherever each sits — which is what lets shared code and its caller bind the same fragments without knowing about each other. Adding a new slot to a template renames no binding that leaves it unfilled.

### Executing and reading

The generator writes one generic base per template, `{Operation}Bound[...]`, with one type parameter per slot in document order. The parameter is the *fragment class* (or a union of them) readable in that slot — `Never` for a slot left unfilled — and `execute` returns the result model parametrized by it, so the type of `result.post.attachment` records which fragments were offered. The base carries `execute` and `with_headers`; each `bind()` gets its own class deriving from it with those parameters filled in:

```python
async def fetch[TFrag](post_id: str, bound: GetPostAttachmentBound[TFrag]) -> GetPostAttachmentResult[TFrag]:
    return await bound.execute(id=post_id)

result = await fetch("1", get_post_attachment_image)
image = image_url.read(result.post.attachment) if result.post else None
```

`execute` takes only the template's own variables. There is no slot keyword argument: the fragment selection already happened at `bind()` time.

Read a slot with the fragment handle itself: `image_caption.read(result.post.attachment)` returns that fragment's own model, or `None` when the node is `None` or its runtime type is outside the fragment's coverage. Any fragment whose fields land on the slot's own payload is readable this way — a fragment bound into the slot, or one another bound fragment spreads next to its own fields:

```python
result = await get_post_attachment_any.execute(id="p-1")
attachment = image_caption.read(result.post.attachment)
```

`fragment.read(node)` is statically checked: the node's type records which fragments its binding offered at that slot, so reading a fragment that binding never offered there — including any read of a node whose slot was left unfilled, which offers nothing at all — is a type error. The same wiring mistakes raise `ValueError` at runtime on type-erased paths — `ValueError` rather than `None`, so a wiring bug cannot look like a legitimate type mismatch.

That check only works where the concrete fragment handle is in scope, as a literal handle or a tuple of them — reading with a handle already erased to `GQLFragment[pydantic.BaseModel]` (a heterogeneous registry, say) is a type error against any node the generator writes, because the erased handle names no fragment the node lists as offered. Only the check is lost, not the data: a handle is an identity token at runtime, so an erased handle its binding really was given still reads back its own model, and the `ValueError` above stays reserved for a handle that binding never offered. Code that is itself generic over which fragments a binding offers has no concrete handle to read with either; it returns the parametrized result and leaves the read to a caller who does know the concrete handles.

A fragment reached through a *narrower* type condition — an interface brick spread inside a per-type fragment — reads back as `None` for the types that condition excludes, exactly like any other type mismatch.

A fragment spread inside a *nested field* of a bound fragment is a different matter: its fields arrive under that field, not on the slot's payload, so the slot cannot read them. Its data comes back through the enclosing fragment's own model (`image_parts.read(node).thumb.alt`), and its handle is not part of the node's offered set, so such a read is a static type error as well (and still a `ValueError` at runtime).

Fragments are isolated from each other — each one reads exactly its own selection, never the fields another caller's fragment selected. The data is not part of the fields of the result model, so `model_dump()` does not include it; re-validating a dump without a fragments context fails immediately.

### Fragment variables

Fragments may spread other fragments and declare their own variables; the generator merges the spread definitions and variable declarations into the expanded operation. Bind the fragment as usual:

```python
image_thumbnail = api_gql("""
    fragment ImageThumbnail on ImageAttachment {
        thumbnail(width: $width)
    }
""")

get_post_attachment_thumbnail = get_post_attachment.bind(attachment=image_thumbnail)
```

Then supply values for the fragment's own variables through `with_args`, a typed method the generator writes on a binding class whenever its fragments declare variables. Call it where the value is known, not as part of the bind:

```python
bound = get_post_attachment_thumbnail.with_args(width=800)
result = await bound.with_headers({"Authorization": "..."}).execute(id="1")
```

`execute` still takes only the template's own variables. Calling it without a value for a required fragment variable raises, naming the variable.

A fragment variable behaves like an operation variable of `execute`: it is a required keyword and passing `None` sends an explicit `null`. The exception is a variable used only in positions the schema gives a default to — the generator relaxes those to optional, gives them a `None` default, and leaves them out of the request entirely when you do, so the schema's default applies. Each `with_args` call states the whole set: a variable you leave out of a later call is left out of the request, whatever an earlier call passed for it.

A fragment the template already spreads by name is a different case: its variables are the template's own, declared by the operation and supplied through `execute`. Binding such a fragment into a slot adds no `with_args`.

### Validation

The runtime validates every fragment readable at each slot at the response boundary — including a slot nobody read and a fragment reached only through another fragment's spread. For queries and mutations, validation happens inside `execute`. For subscriptions, it happens on every received message.

### What the generator rejects

Generation fails, naming the file and line, for:
- a `.bind()` call the scan cannot read statically: a positional or `**kwargs` argument, a repeated slot keyword, a slot value that is neither a name nor an inline `api_gql("...")` call nor a list of those, or a template reached through an attribute chain into the scanned tree (`import infra` then `infra.template.bind(...)`) instead of a directly imported name;
- a name a bind reads that is bound more than once in the scope it resolves in — two assignments, two imports, or a mix; which of them it means is a question the scan will not guess at;
- a slot value that does not resolve to a discovered fragment statement. The template a bind hangs off is judged differently, because `.bind()` is an ordinary method name that sockets and widgets carry too: a base that resolves to nothing at all leaves the call alone, and only a base whose name the scanned tree does hold as a statement is an error — reported with that statement's own address, since a template written where no resolution reaches it (a class body, a star import) is ours all the same. A name something else *does* bind where the call stands is answered by that binding and left alone: a same-named statement elsewhere in the tree is a coincidence, and two generator runs over one tree produce it routinely;
- a source file the scan cannot parse that mentions the gql function or `bind` — it may hold statements or binds, and dropping it would rewrite the package without them. A file that names neither owns nothing the package could lose and is skipped, unless some bind's name resolution has to travel through it;
- a fragment that is not spread-compatible with the slot — the same mismatch is also a type error at the `bind()` call site, so most callers see it in the editor before they even regenerate;
- two combinations whose slot and fragment names derive the same class name — rare, and the fix is to alias the slot field or rename one of the fragments (writing the *same* combination twice is not an error: it is one binding, one class, and both call sites are listed on it);
- a fragment readable at a slot's root reached under `@skip`/`@include` — whether the directive sits on the spread or on an inline fragment around it — at any runtime type where that conditional path is the only way it reaches the root, since such a fragment is requested and validated on every response and so cannot be conditionally absent. Reaching it unconditionally as well — bound directly, or spread again without a directive — covers the types reached that way, and only the types left over are rejected. The same directive on a spread inside a nested field is fine;
- a bound fragment whose name a template also defines locally, with a different definition — one name is one definition in the expanded document;
- two slots of one template whose names collapse to the same Python name or to the same type parameter — a slot's name is its `bind()` keyword and the type parameter it contributes to the operation's bound base and result model;
- `@slot` inside a fragment definition, or a slot nested inside another slot's own selection.

One slot name selected under two parents is not an error: both positions carry the same spliced fragments, so every fragment reachable from either position reads the same way through its own handle. A slot on a polymorphic parent works the same way — one node model per variant.

A stale bind — a fragment combination the generator never saw — raises `LookupError` where the `bind()` call runs: at import for a module-level bind, at call time for one inside a function. Some calls can even pass type-checking without matching any discovered bind — types cannot count list elements, so a list bind that uses a strict subset of another list's fragments looks like a valid call statically. Such a call raises the same `LookupError` at import; regenerating after fixing it resolves it.

Passing the same fragment to one slot twice is rejected at generation instead, naming the call site: a slot spreads each of its fragments once, so `bind(slot=[f, f])` asks for a combination that cannot exist.

A `.bind()` the scan leaves alone builds no binding class, so its call site raises that same `LookupError`. Two kinds land there: a third-party `.bind()`, which is none of the generator's business, and one of ours written on an expression no scan can read — `TEMPLATES["q"].bind(...)`, where the value under the key is a runtime question. Both are recorded with the reason they were left alone; `debug_path` writes them to `ignored_binds.json` alongside the other debug artifacts, so "left alone on purpose" and "lost" stay distinguishable.

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
