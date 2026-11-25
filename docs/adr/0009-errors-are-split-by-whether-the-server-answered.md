# The client's errors are its own, split by whether the server answered

An operation that yields no result raises a type of iron_gql under one root, `GraphQLClientError`. The first split is whether the server's answer arrived: `TransportError` when it did not arrive whole, with `SubscriptionInterruptedError` under it for a stream that was accepted and broke before `complete`, and `ResponseError` when the server answered with something other than a result. Under `ResponseError` each kind of answer has a type that holds its evidence: `GraphQLResponseError` the server's `errors`, `HTTPResponseError` the status and the response, `WebSocketClosedError` the close code, `ResponseProtocolError` a diagnosis of what breaks the protocol. Queries, SSE and WebSocket subscriptions raise the same types in both clients.

Until now the exceptions of `httpx2` passed through. They are not one model: those of `httpx2.websockets` do not derive from `httpx2.HTTPError`, and a closed socket or a missing `connection_ack` has no `httpx2` exception, so a client that speaks three protocols cannot be consistent in them. A caller that retries an unreachable server and nothing else had to take the transport of the library apart, and could not tell a 503 from a 401 when both carried a GraphQL body. ADR 0008 opens the settings of `httpx2` to a consumer, not its exceptions. The original exception stays as `__cause__`.

- `errors` holds only what the server sent and is never empty. What the client finds wrong itself is a `ResponseProtocolError`, not an error made up in the server's name.
- `status_code` belongs to `ResponseError`. `response` belongs to `HTTPResponseError` alone, where the body and the headers are the only explanation there is. It keeps the request with its headers alive, `Authorization` included, as `httpx2.HTTPStatusError` did.
- A refused WebSocket upgrade is an `HTTPResponseError` whose body is not read: `httpx2` closes the stream before the caller could read it.
- A WebSocket close with a code from 4000 to 4999 is a refusal, since graphql-transport-ws refuses with such codes. Code 1009 is a protocol error, since `httpx2` reports an oversized message with it and opening the stream again does not help. Every other close, 1000 among them, is a broken connection, as the end of an SSE response without `complete` is: only `complete` ends an operation.
- A wrong call or setting raises a built-in exception, and data that does not fit the generated models raises `pydantic.ValidationError`, as before.
- No type says that a retry is safe. A server may have executed a mutation whose answer did not arrive.

## Considered Options

- **Keep the exceptions of `httpx2` and add the status to `GraphQLResponseError`** — the smallest change, and no consumer rewrites an `except`. Callers check three types, and WebSocket failures stay outside, or become `httpx2` exceptions made up for them. Rejected.
- **One response type with a possibly empty `errors`** — one class less, but every reader of `errors` has to check that it is not empty, and `errors[0]` fails on a 502. Rejected.
- **No `response` at all, only `status_code`** — no request stays in the exception, but the body of a 502 or of a 400 without a GraphQL body is lost to diagnosis, and so is `Retry-After`. Rejected.
- **Read the body of a refused upgrade in a response hook** — recovers GraphQL errors from it, but adds a hook of the client's own beside the consumer's. Rejected.
- **Split closes by the moment, before and after `connection_ack`** — no ranges of codes, but a server restarting before `connection_ack` reads as a refusal, and access revoked in a stream as a broken connection. Rejected.
- **Keep every exception from a consumer's `auth` or hook as it is** — telling a connection failure inside a callback from one of the operation takes tracking where each exception came from. A connection failure of `httpx2` in a callback is a `TransportError`; every other exception of a callback passes as it is. Rejected for complexity.

Reason to revisit — a consumer needs the headers or the body of an answer that is not an `HTTPResponseError`, such as `Retry-After` of a 503 with GraphQL errors.
