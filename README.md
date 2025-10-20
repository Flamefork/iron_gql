# iron_gql

`iron_gql` is a lightweight GraphQL code generator and runtime that turns schema SDL and real query documents into typed Python clients powered by Pydantic models. Use it to wire GraphQL APIs into services, CLIs, background jobs, or tests without hand-writing boilerplate.

## Key Features
- **Query discovery.** `generate_gql_package` scans your codebase for calls that look like `<package>_gql("""...""")`, validates each statement, and emits a module with strongly typed helpers.
- **Typed inputs and results.** Generated Pydantic models mirror every selection set, enum, and input object referenced by the discovered queries.
- **Async runtime.** `runtime.GQLClient` speaks to GraphQL endpoints over `gql` + `httpx` and can shortcut network hops when pointed at an ASGI app.
- **Deterministic validation.** `graphql-core` enforces schema compatibility and rejects duplicate operation names with incompatible bodies.

## Package Layout
- `generator.py` – orchestrates query discovery, validation, and module rendering.
- `parser.py` – converts GraphQL AST into typed helper structures consumed by the renderer.
- `runtime.py` – provides the async `GQLClient`, the reusable `GQLQuery` base class, and value serialization helpers.

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

   from iron_gql.generator import generate_gql_package

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

## Customization Hooks
- **Scalar mapping.** Provide `scalars={"DateTime": "datetime:datetime"}` to map schema scalars onto importable Python types. Unknown scalars fall back to `object` with a log warning.
- **Naming conventions.** Supply `to_camel_fn_full_name` (module:path string) and a `to_snake_fn` callable to align casing with your own `alias_generator`.
- **Endpoint configuration.** `base_url_import` is written verbatim into the generated module; point it at a global string, config object, or helper that returns the GraphQL endpoint.

## Runtime Highlights
- `GQLClient` accepts ASGI `target_app` so you can reuse the runtime for production HTTP calls or in-process ASGI execution.
- `GQLQuery.with_headers` and `GQLQuery.with_file_uploads` clone the query object, making per-call customization trivial.
- `serialize_var` converts nested Pydantic models, dicts, lists, and primitives into JSON-friendly structures for variable payloads.

## Validation and Troubleshooting
- Errors identify the file and line where the problematic statement lives.
- Duplicate operation names must share identical bodies; rename or consolidate to resolve the conflict.
