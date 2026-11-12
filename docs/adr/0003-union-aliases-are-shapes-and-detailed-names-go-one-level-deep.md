# A result union alias is named by its shape, and a detailed name expands one level

A result union alias is a shape just like a model: result union aliases with identical contents from different paths are one, and the parent refers to its shape, not its path. The bare name of an abstract type (`Media`) goes to the only shape of that type in the module; models and result union aliases of one GraphQL type claim it together, otherwise an interface's fallback model could silently take the name a result union alias left. Other result union alias shapes get `{AbstractType}_<digest>`. A model's detailed name expands nested shapes one level deep: a nested model's token is its `{Type}With{Fields}`, a result union alias's token is the abstract type's name, and list and nullable wrappers stay tokens; collisions are told apart by a suffix from the structural key (ADR 0001). This way the name stops growing with selection depth and does not carry a path with an anonymous operation's hash, and one result union alias shape no longer sits in the module under several names.

## Considered Options

- **A result union alias name from the path for a shape that stands in one path** — keeps today's imports of such result union aliases, but leaves the anonymous operation's hash in names and requires holding the path claim forever, otherwise the freed name passes to another shape. Rejected.
- **Always `{AbstractType}_<digest>`** — simpler, but the only shape loses its readable name. Rejected.
- **A length cap on the detailed name** — `{Type}With{Fields}_<digest>` beyond N characters does not guarantee N (`{Type}With{Fields}` alone is already long), and a hard limit requires deterministic truncation. Rejected: after one level the name's length does not grow with selection depth, so we introduce no cap.
- **Always `{Type}With{Fields}_<digest>` instead of the detailed name** — shorter, but the name does not show how the shapes of one short-name claimant differ. Rejected.

Reason to revisit — after one level, detailed names in consumers still get in the way of reading the module or basedpyright messages: then a cap with truncation.
