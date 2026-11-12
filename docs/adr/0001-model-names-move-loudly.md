# A model name moves to another shape only loudly

The generator shares one result model among all of a module's queries that have the same selection shape, so a model's name depends on more than its own query: a new query can take a model's short name away. We accept this coupling, but editing one query must never silently change another query's typing — a name that code already imports cannot start meaning a different shape while the previous shape still exists. So every naming rule assigns a name in one of two ways: as a function of the shape itself (the detailed name, the tokens of a `__typename` literal, a suffix from the shape's structural key) or only to a sole claimant (the short `{Type}With{Fields}`, the bare `{Type}`, a name from the path where the shape stands in exactly one place). When there is more than one claimant, nobody gets the name, and the previous owner loses it loudly — the import fails.

The phases that decide whether to merge models and whether to grant a shortening compare shapes by structural key, not by matching names: equal names of different shapes are told apart by a suffix from the key, not by a generation error.

## Considered Options

- **A name from the difference between a short-name claimant's shapes** — shorter, but depends on neighboring shapes: a new shape can move the previous name to another one. Rejected.
- **An ordinal suffix `_2` and "the first path in sorted order"** — a silent name move: shape S moves to another name, a new shape S′ takes the freed one, and code with the old import does not fail. Rejected.
- **The name is always a pure function of the shape, with no shortenings** — full isolation between queries, but nested selections that live code imports would get a hash. Rejected in favor of simple names.
- **Names by operation and path** — isolation, but no classes shared between queries, and anonymous operations carry a hash in the name. Rejected.

Reason to revisit — a model name must stay unchanged when an unrelated query is added (stability, not just loudness): that calls for a persistent name registry or giving up shortenings.
