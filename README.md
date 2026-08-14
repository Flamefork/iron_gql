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

Statement ровно с одним fragment definition возвращает типизированное значение **fragment definition**, если пакет содержит template. Каждый вызов `api_gql()` создаёт новое эквивалентное значение с тем же generated-типом. В пакете без template нет binding context, поэтому fragment statements сохраняют прежний смысл. Генератор создаёт базовый `On{Type}` для каждого используемого type condition. Plain definitions и factory applications наследуют базу своего type condition; инфраструктурный код может принимать через неё любой bindable fragment на этом типе. Statement с несколькими definitions остаётся bundle, чьи fragments можно spread по имени.

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

`bind` is an ordinary method call, not a statement the generator has to read. Every combination of a template with a fragment compatible with its slot is generated from the schema, whether or not any call site writes it — so the fragment can arrive as a function parameter, and the template it fills need never be exported at all:

```python
async def attachment_of[TModel: pydantic.BaseModel, TReads](
    post_id: str, details: OnImageAttachment[TModel, TReads]
) -> TModel | None:
    bound = get_post_attachment.bind(attachment=details)
    result = await bound.execute(id=post_id)
    return details.read(result.post.attachment) if result.post else None
```

The caller passes its own fragment and gets back that fragment's own model. The helper never names the fragment, and its own module can keep the template private — nothing outside has to see it:

```python
caption = await attachment_of("p-1", image_caption)
```

Для этого нужны базы `On{Type}`. Helper для любого fragment на `ImageAttachment` принимает `OnImageAttachment[TModel, TReads]` и возвращает `TModel`; helper, который возвращает caller весь result, выражает тип result через оба параметра fragment. В обоих случаях сохраняется точный тип: модель fragment уникальна для его definition, поэтому данные другого fragment нельзя прочитать из slot по ошибке.

A bind can also be written literally, with the template and fragments local to the function or written straight into the call:

```python
async def show_attachment(post_id: str) -> None:
    bound = get_post_attachment.bind(attachment=api_gql("""
        fragment ImageAlt on ImageAttachment {
            alt
        }
    """))
    result = await bound.execute(id=post_id)
```

A slot can also take a tuple of fragments. A tuple is the one form the generator still reads from your source: single fragments are enumerated from the schema, but every subset of the compatible fragments is not (that is exponential), so a multi-fragment combination exists only where a call site writes it literally. A tuple and not a list, because the overload written for it names a fixed length: a list of two fragments would also admit a list of one, and the phantom would then offer a fragment that shorter binding never bound. Pass a list and the generator says so, naming the call site. Their covered runtime types may overlap — each fragment reads its own slice of the payload independently. What the generator still rejects is a field-merge conflict in the expanded operation (the same response key selected with different arguments), reported by standard GraphQL validation. A fragment bound alone to a slot and the same fragment inside a tuple bind of that slot coexist:

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
    attachment=(image_caption, link_summary)
)
```

Slots you do not name in `bind()` stay unfilled: the operation still requests their static `__typename` selection, but no fragment data comes back for them. Omitting a slot and passing it an explicit empty sequence (`attachment=()`, or `attachment=[]`) mean the same thing — an empty one names no fragment, so nothing is written for it that a shorter call could land on. The same fragment can be bound into any number of templates, and a template can have any number of binds.

A binding **is** its combination: the template and the fragments in each slot. The generator derives empty and single-fragment variants from the schema. It also adds multi-fragment combinations that call sites specify as literals. Each template stores one binding spec for every canonical combination, so duplicate call sites share one spec.

The limit of 256 applies to the final canonical set for each template. This set includes schema-derived and literal multi-fragment combinations after deduplication. If the set exceeds the limit, the error reports the total and the schema and literal contributions. Split the template or narrow its slot types.

### Executing and reading

The generator writes one class per template, `{Operation}Bound[TResult]`, with a single type parameter: the result this binding returns. `bind()` returns an instance of it carrying that combination's own operation text, and its `execute` answers with exactly the result the parameter names. Which fragments are readable in each slot is recorded by the result type itself, so `result.post.attachment` still knows what was offered — a slot left unfilled is statically unreadable. Code that works over any binding of one template takes the class with its parameter open and hands the result back to a caller who knows which binding it passed:

```python
async def fetch[TResult: pydantic.BaseModel](post_id: str, bound: GetPostAttachmentBound[TResult]) -> TResult:
    return await bound.execute(id=post_id)

result = await fetch("1", get_post_attachment_image)
image = image_url.read(result.post.attachment) if result.post else None
```

The result models belong to the template, not to each binding. `GetPostAttachmentResult` and its nested models have one generic type parameter per slot. A binding fills these parameters, for example `GetPostAttachmentResult[ImageUrl | LinkUrl]`. Thus, one template writes one set of models for all its bindings, and a validation error names that shared model rather than any one combination. A helper can use `GetPostAttachmentResult[Any]` to accept the result of any binding (see below).

Pickle support depends on which generic models the payload instantiates. A populated path to a slot creates nested Pydantic parametrizations without module-level pickle names. `pickle.dumps()` raises `PicklingError` for such a result. If `None` stops every slot path before a nested generic model is created, only the registered root specialization is instantiated. `{"post": null}` creates no nested model, so its result can cross a process boundary.

`execute` takes only the template's own variables. There is no slot keyword argument: the fragment selection already happened at `bind()` time.

Читайте slot через fragment definition: `image_caption.read(result.post.attachment)` возвращает собственную модель fragment или `None`, если node равен `None` либо его runtime type не входит в coverage fragment. Так читается любой fragment, чьи поля попали непосредственно в payload slot: переданный в `bind()` fragment или fragment, который другой bound fragment spread рядом со своими полями. Новый эквивалентный definition из повторного вызова `api_gql()` читает тот же result:

```python
result = await get_post_attachment_any.execute(id="p-1")
same_definition = api_gql("""
    fragment ImageCaption on ImageAttachment {
        caption
    }
""")
attachment = same_definition.read(result.post.attachment)
```

`fragment.read(node)` проверяется статически: тип node хранит definitions, предложенные binding для этого slot. Чтение чужого definition — включая любое чтение незаполненного slot — является type error. На type-erased пути та же ошибка связи поднимает `ValueError`, а не возвращает `None`, чтобы ошибка wiring не выглядела как допустимое несовпадение runtime type.

Эта проверка работает, пока сохранён точный тип definition или application. Для heterogeneous registry укажите оба type-параметра явно: `GQLFragment[pydantic.BaseModel, Any]`. Если точный readable-контракт уже стёрт, используйте и `{Operation}Result[Any]`: он принимает result любого binding и поддерживает тот же `read`. Теряется только статическая проверка; runtime по-прежнему допускает лишь definition, который binding предложил для slot.

Так общая инфраструктура может владеть operation и возвращать данные вместо сырого result: helper указывает `Any` для неизвестного ему slot и читает через переданный caller fragment.

```python
def title[TData: pydantic.BaseModel, TReads](
    result: GetPostAttachmentResult[Any], fragment: GQLFragment[TData, TReads]
) -> TData | None:
    return fragment.read(result.post.attachment) if result.post else None
```

A fragment reached through a *narrower* type condition — an interface brick spread inside a per-type fragment — reads back as `None` for the types that condition excludes, exactly like any other type mismatch.

A fragment spread внутри *nested field* bound fragment ведёт себя иначе: его поля приходят под этим field, а не в payload slot, поэтому slot не может читать их напрямую. Данные доступны через модель enclosing fragment (`image_parts.read(node).thumb.alt`); nested definition не входит в offered set node, поэтому прямой `read` остаётся static type error и поднимает `ValueError` в runtime.

Fragments are isolated from each other — each one reads exactly its own selection, never the fields another caller's fragment selected. The data is not part of the fields of the result model, so `model_dump()` does not include it; re-validating a dump without a fragments context fails immediately.

### Fragment variables

Fragments могут spread другие fragments и использовать собственные variables. Генератор синтезирует для каждой variable GraphQL declaration, допустимый во всех местах использования. Если такого declaration нет, generation завершается ошибкой. Fragment, closure которого использует variable, становится **fragment factory**. Сам definition factory нельзя передать в `bind()`; типизированный `with_args` создаёт bindable **fragment application** со значениями variables:

```python
image_thumbnail = api_gql("""
    fragment ImageThumbnail on ImageAttachment {
        thumbnail(width: $width)
    }
""")
```

Вызывайте `with_args` там, где известны значения, и передавайте application в `bind()`. Читать result можно через definition factory или через любую application той же factory; `read()` проверяет definition, но не происхождение значений arguments:

```python
async def show_thumbnail(post_id: str, width: int) -> None:
    bound = get_post_attachment.bind(
        attachment=image_thumbnail.with_args(width=width)
    )
    result = await bound.with_headers({"Authorization": "..."}).execute(id=post_id)
    if result.post is not None:
        image = image_thumbnail.read(result.post.attachment)
```

Обязательной fragment variable соответствует обязательный keyword в `with_args`. `None` всегда отправляет явный GraphQL `null`. Если у всех usage positions есть schema default, keyword можно пропустить: variable не попадёт в request, и применится schema default. Omission и `None` — разные состояния. Точная сигнатура `with_args` отвергает пропущенные обязательные и лишние keywords. Набор variables в application неизменяем. Applied class остаётся private; создавайте applications только через `with_args`.

Каждый `with_args` возвращает новую application. Applications одной factory могут содержать разные arguments, но читают одну и ту же проекцию. `read()` завершится ошибкой только для definition другой factory или другого fragment.

Two applications of one factory can fill two slots of one template. They read their own slots, but they cannot supply two independent values for one GraphQL variable. If the applications disagree, `bind()` reports the variable and both sources without showing either value. Use the same value, or use two fragments with different names.

If a template already spreads a factory by name, the operation owns that factory's variables and `execute` supplies them. A second source exists only when the factory can also bind to a compatible slot of that template. The generator rejects this reachable conflict. A static spread is valid when the template has no compatible slot.

### Validation

The runtime validates every fragment readable at each slot at the response boundary — including a slot nobody read and a fragment reached only through another fragment's spread. For queries and mutations, validation happens inside `execute`. For subscriptions, it happens on every received message.

### What the generator rejects

The scanner reads a complete combination only when a `.bind()` call contains a non-empty literal sequence or a literal `**{...}` mapping. A multi-fragment tuple is the only combination that the schema cannot derive. The scanner also reads a non-empty list to report that it has no fixed length. Empty `()` and `[]` values contain no static fragment. The scanner checks their keyword names but does not resolve adjacent single values. A named `**opts` mapping is also unresolved because its contents are not statically known. The scanner ignores calls where every slot receives one value. This rule lets a helper bind a fragment that it receives as a parameter. Therefore, the following rules apply to tuple bindings.

Generation fails, naming the file and line, for:
- a tuple `.bind()` call the scan cannot read statically: a positional argument, a `**` spread (the resolver has no keyword name to thread through, whether the spread stands alone or beside another slot's tuple), a repeated slot keyword, a slot value that is neither a name nor an inline `api_gql("...")` call nor a tuple of those, or a template reached through an attribute chain into the scanned tree (`import infra` then `infra.template.bind(...)`) instead of a directly imported name. Once a call is read at all, every one of its slots has to resolve — the binding key is built from the whole call, so a resolvable tuple beside an unresolvable single value is an error rather than a partial read;
- a slot given a non-empty list rather than a tuple: no fixed-length overload can be written for a list, so one written for two fragments would accept one of them and offer a phantom that binding never bound. An empty list names no fragment and stays legal;
- a name such a bind reads that is bound more than once in the scope it resolves in — two assignments, two imports, or a mix; which of them it means is a question the scan will not guess at;
- a slot value of such a bind that does not resolve to a discovered fragment statement. The template it hangs off is judged differently, because `.bind()` is an ordinary method name that sockets and widgets carry too: a base that resolves to nothing at all leaves the call alone, and only a base whose name the scanned tree does hold as a statement is an error — reported with that statement's own address, since a template written where no resolution reaches it (a class body, a star import) is ours all the same. A name something else *does* bind where the call stands is answered by that binding and left alone: a same-named statement elsewhere in the tree is a coincidence, and two generator runs over one tree produce it routinely;
- a template whose final deduplicated set of schema-derived and literal combinations exceeds 256. The error reports the total and both contributions;
- a source file the scan cannot parse that mentions the gql function or `bind` — it may hold statements or binds, and dropping it would rewrite the package without them. A file that names neither owns nothing the package could lose and is skipped, unless some bind's name resolution has to travel through it;
- a fragment that is not spread-compatible with the slot — the same mismatch is also a type error at the `bind()` call site, so most callers see it in the editor before they even regenerate;
- a generated name claimed twice by an actual `On{Type}` base, a factory, a private Applied class, a fragment, a model, or an enum. A fragment name can also collide with a type parameter in another slot's `bind()` overload. Rename the fragment or add an alias to the field that creates the model;
- a fragment readable at a slot's root reached under `@skip`/`@include` — whether the directive sits on the spread or on an inline fragment around it — at any runtime type where that conditional path is the only way it reaches the root, since such a fragment is requested and validated on every response and so cannot be conditionally absent. Reaching it unconditionally as well — bound directly, or spread again without a directive — covers the types reached that way, and only the types left over are rejected. The same directive on a spread inside a nested field is fine;
- a bound fragment whose name a template also defines locally, with a different definition — one name is one definition in the expanded document;
- a factory that a template spreads by name and can also bind to a compatible slot in that template. `execute` and `with_args` would become two sources for one GraphQL variable;
- two slots of one template whose names collapse to the same Python name — a slot's name is its `bind()` keyword, and one keyword cannot mean two slots;
- `@slot` inside a fragment definition, or a slot nested inside another slot's own selection.

One slot name selected under two parents is not an error: both positions carry the same spliced fragments, so every fragment reachable from either position reads through its definition or application. A slot on a polymorphic parent works the same way — one node model per variant.

A combination with no text of its own raises `LookupError` where the `bind()` call runs: at import for a module-level bind, at call time for one inside a function. Single-fragment and empty combinations always exist now, so only tuple binds can land there, in three ways. A tuple written after the last regeneration: regenerating resolves it. A call the overload form admits without a combination behind it: one form covers each arity a slot has tuples for, fixing the length and the fragments a position may hold — every fragment any tuple of that arity wrote for the slot — but not which of them goes where, so a repeat like `(f, f)` or a pair drawn from two different tuples of one slot type-checks — as does a call mixing a single fragment in one slot with a literal tuple in another, which meets the two slots' separate forms while no combination pairs them. (A shorter tuple standing in for a longer one is not among them: the arity is fixed.) Each of those needs its combination written literally, if it is one at all. And a call the scan cannot read at all, whose template is an expression rather than a name: that one is written and still never generated, so the message points at `ignored_binds.json` instead, where the call is recorded with its reason.

Passing the same fragment to one slot twice is rejected at generation instead, naming the call site: a slot spreads each of its fragments once, so `bind(slot=(f, f))` asks for a combination that cannot exist.

Every `.bind()` the scan leaves alone is recorded with the reason, and `debug_path` writes them to `ignored_binds.json` alongside the other debug artifacts. Most entries there are now ordinary: a call whose slots take single values needs nothing from the scan, and a third-party `.bind()` is none of the generator's business. The one worth reading the file for is a *tuple* bind written on an expression no scan can follow — `TEMPLATES["q"].bind(slot=(a, b))`, where the value under the key is a runtime question: its combination is never generated, so the call raises `LookupError`, and the reason recorded here is what tells that apart from a bind the generator lost.

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

The [`example/`](example/) directory reads as chapters. Each one adds what the one before it did not need:

| Chapter | What it adds |
|---|---|
| [`ch01_queries.py`](example/ch01_queries.py) | a query, a fragment, nested fields, `with_headers` |
| [`ch02_mutations.py`](example/ch02_mutations.py) | a generated input model and an enum |
| [`ch03_polymorphism.py`](example/ch03_polymorphism.py) | a union and an interface, matched by generated class |
| [`ch04_variables.py`](example/ch04_variables.py) | `@include`/`@skip`, and a `@oneOf` input |
| [`ch05_scalars.py`](example/ch05_scalars.py) | a `DateTime` that arrives as `datetime`, an `Upload` sent as a `FileVar` |
| [`ch06_slots.py`](example/ch06_slots.py) | a template, `bind`, `read`, and fragment variables through `with_args` |
| [`ch07_subscriptions.py`](example/ch07_subscriptions.py) | a subscription stream |
| [`ch08_sync.py`](example/ch08_sync.py) | the basics again, against a synchronous package |

A real project picks one mode; the example shows both, and [`generate.py`](example/generate.py) writes both packages from the one schema.

The chapters are written to be read and type-checked, not run: they point at a server that this repository does not ship. [`test_example.py`](example/test_example.py) is the exception. It runs chapters against [`fake_app.py`](example/fake_app.py) with the helpers from [Testing](#testing).

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

`use_client` binds your client into a generated package. Use it with `async with` for `AsyncGQLClient` and `with` for `GQLClient`. On exit it restores the previous client and closes the client that you passed in:

```python
from iron_gql.runtime import AsyncGQLClient
from iron_gql.testing import use_client
from myapp.gql import api

async def test_get_user():
    client = AsyncGQLClient(base_url="http://testserver", target_app=my_asgi_app)
    async with use_client(api, client):
        result = await get_user.execute(id="1")
        assert result.user.name == "Alice"
```

### Serve an app on a loopback port

`live_asgi_server` serves an ASGI app with `uvicorn` on a port that the operating system picks, and it yields the URL of that server:

```python
from iron_gql.runtime import GQLClient
from iron_gql.testing import use_client
from iron_gql.testing.server import live_asgi_server

def test_get_user_sync():
    with (
        live_asgi_server(my_asgi_app) as base_url,
        use_client(api, GQLClient(base_url=base_url)),
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
