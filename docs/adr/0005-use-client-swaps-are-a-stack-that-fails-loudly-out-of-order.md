# `use_client` swaps are a stack of per-entry records, and an exit out of entry order is a loud error

`use_client` sets its own client on a module, and on exit restores the previous one and closes its own. Remembering a single previous client is not enough: if swaps of one module overlap (enter A, enter B, exit A), exiting A restores the original client while B is still active, and exiting B sets the already closed client A on the module, so every later request of the module fails. So `use_client` keeps a registry: per module, the original client and ordered records, one per entry rather than per client. An exit removes its own record wherever it stands, binds the client of the last remaining record or the original one, and closes its own. Nested swaps are a supported contract. An exit out of entry order raises `RuntimeError` in the test that exited, after this restoration: the restoration is there so that only the culprit fails, but without guarantees like trio's. An entry refused because of a foreign model base records nothing and does not close the client: ownership did not transfer. The registry lives in `iron_gql.testing`, not in the module's namespace. A swap applies to the whole process; we do not promise isolation of concurrently running scenarios or concurrent entries and exits of one module's swaps from different threads — there is no lock.

## Considered Options

- **One remembered previous client, like `unittest.mock.patch` and `ContextVar.reset`** — on overlap it silently leaves a stale value; that is how it was before this decision. Rejected.
- **Forbid overlap with a check on exit without a registry, like anyio** — anyio raises an error on exit from a scope that is not the current one and leaves the state untouched; the module stays with the binding the violation left, and not only the culprit test fails. Rejected.
- **Leave it as is and document it in the README** — the module silently stays with a closed client. Rejected.

Reason to revisit — a consumer needs to swap one module in concurrently running scenarios of one process.
