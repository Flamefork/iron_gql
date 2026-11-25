# An operation's options refine the client's, with one limit per step

The operations of one module need different limits: a heavy query waits longer than the default, and a mutation the caller repeats within a deadline waits less. `with_options` gives an operation the options its client has under the same names and with the same meaning: the client's are the defaults, and the operation's refine them. Both layers, and two `with_options` in a row, merge by one rule: the dict options, `headers` and `httpx_options`, merge by key, and any other option is replaced. `with_timeout` and `with_headers` are `with_options` with one option.

The limits are those of the steps every operation goes through, whatever its kind:

- `connect_timeout` — each step of reaching the server and sending the operation.
- `timeout` — each wait for the server's answer: the response of a query or a mutation, the opening of a subscription, which over WebSocket goes up to `connection_ack`.
- `stream_idle_timeout` — each silence in an open stream.

Until now the client had `query_timeout` and `subscription_idle_timeout`, named after kinds of operation. The first limited connecting and sending too, so a query given longer to wait was also slower to notice a server that is down, and the second put the opening of a subscription under the limit of its silence, which is none by default. At an operation, which has one kind, one of the two would always mean nothing.

- An operation's `httpx_options` holds `auth`, `follow_redirects` and `extensions` only. The client sets the rest of a request itself, and `extensions` with `timeout` in it is refused, since `httpx2` would put it over the limits above without a word.
- `httpx2` has one read timeout for the response headers and for the body. To limit the opening of an SSE stream and its silence apart, the client changes the read timeout in the request's `timeout` extension once the stream is accepted. `httpx2` does not promise to read it again; 2.7.0, 2.10.0, 2.13.1 and 2.16.2 do. If a version stops, `timeout` stays on the open stream and ends a quiet one.
- There is no limit on an operation as a whole: each limit holds for each wait of its step.

## Considered Options

- **The client's two options at the operation, the one of the other kind unread** — the smallest change, and it keeps the faults above. `with_timeout` at a subscription would limit its connecting only. Rejected.
- **Options per kind at the operation** — the option of the other kind is an error, but the base class of operations splits in two, and the names of the limits differ between the client and the operation. Rejected.
- **One `timeout` for the answer and the silence, with options per kind over it** — three more client options, and a client-wide `timeout` raised for the queries ends a quiet subscription. Options for subscriptions alone would put a subscription on another level than a query and a mutation, which it is not. Rejected.
- **`httpx2.Timeout` as a value** — says the steps apart in the words of `httpx2`, but one number would still mean all of them. Rejected.
- **Limits as arguments of `execute`** — its keyword arguments are GraphQL variables. Rejected.
- **A short method per option** — `with_headers` and `with_timeout` are the ones operations need; the other options go through `with_options`. Rejected.

Reason to revisit — `httpx2` gives the headers and the body of a response separate read timeouts, or stops reading the request's `timeout` extension again.
