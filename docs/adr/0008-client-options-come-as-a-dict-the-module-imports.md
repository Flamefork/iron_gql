# Client options come as a dict the module imports

A generated module creates its own client at import (ADR 0002), so a consumer has no call in which to pass that client a timeout, a header or a TLS setting. The generator takes `client_options_import`, a `module:name` like `base_url_import`, and the module passes the dict it names to its client as keyword arguments. The dict is typed by `GQLClientOptions` or `AsyncGQLClientOptions`, a `TypedDict` of the client's options. One of them, `httpx_options`, goes on to the `httpx2` client underneath. A new client option is a constructor parameter and a key of the type; the generator does not change.

`httpx_options` makes the parameters of `httpx2` part of what a consumer writes, and we accept that a new version of `httpx2` can break such a consumer without a change in iron_gql. It also lets a consumer change behavior the README describes: `follow_redirects` ends the rule that a 3xx raises, and `auth` overwrites the `Authorization` header of an operation. The client refuses the keys it owns: `base_url`, `headers`, `timeout` and `cookies`, which it sets itself, and `transport` and `mounts`, because the async client opens an `httpx2` client per event loop, and the first one to close would close a transport they share. It refuses `proxy` together with `target_app`, because `httpx2` would send the requests to the proxy instead of the app.

## Considered Options

- **A generator parameter per client option** — every option of the client repeats in the generator and in the rendered module, and its value is fixed when the module is generated, unless each parameter is an import of its own. Rejected.
- **Only the client's own options, without `httpx_options`** — iron_gql answers for the meaning of every option in every mode, but a client certificate, the pool limits or HTTP/2 wait for a release of iron_gql. Rejected.
- **One dict with the endpoint and the subscription protocol too** — one place for everything about the client, but every consumer rewrites its generator call, including those that need no option. Rejected.
- **A client the consumer creates** — the most general way, and it needs no type for the dict. It gives the client an owner outside the module, which ADR 0002 avoids so that nobody has to think about the lifetime of connections. Rejected.
