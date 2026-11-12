# A surviving mutant is evidence to look into, not a defect; mutation runs report and never gate

Mutation testing finds behaviour changes the tests do not notice, which coverage cannot answer. A surviving mutant is evidence for review, not a proven defect: one survivor is a weak check, another an equivalent change no test can tell apart, another a branch unreachable by construction, another the wording of a message nobody depends on. So `just mutants` and `just mutants-diff` report the mutants left unkilled, grouped by status, and fail only when the run itself does -- the suite fails before mutating, a command fails, or no report comes out; a run that left mutants unchecked says it is incomplete. The checks that gate a change stay the main suite, lint, types and the coverage floor.

A survivor confirmed as a real gap becomes a regression test through the public generator and runtime, like any other test, and that test is what then holds the behaviour. Mutants of one untested behaviour are one finding, not one per mutant. Code is simplified when the change improves it on its own terms, not to move the count. Equivalent mutants, invariants and accepted minor differences may stay in the report; an unexamined survivor is not thereby equivalent, and equivalence is argued over the valid inputs, not from the absence of a killing test. Zero survivors and a fate for every mutant are not criteria for finishing work. The `raise AssertionError` pattern in `[tool.mutmut]` stays, as the coverage exemption applied to mutation, because it keeps unreachable lines out of the report.

A test of a diagnosis checks what the diagnosis promises: its type, place, diagnosis, hint, cause chain and machine-readable keys. Comparing the full text is allowed and often the simplest way to check all of that at once, but not required for its own sake, to kill a mutant that changes letter case.

Review of survivors runs on a budget, starting from the behaviours where a gap costs most -- aliases, defaults, directives, graph walks -- and what the budget does not reach stays in the report rather than in a ticket per mutant.

## Considered Options

- **Zero survivors everywhere, every mutant given a fate (the previous form of this ADR)** -- reaching it in `iron_gql.testing` took timing thresholds, pragmas and code reshaped for the tool, and the codegen area alone held about 500 survivors, a large share of them equivalents, unreachable residue of invariants and wording. A green count did not show that any of that work improved the tests. Rejected.
- **Gate only on the functions a branch changed** -- shorter, but keeps the noise, and misses the effect a change has through its callers and through test-only changes. Rejected.
- **A committed list of known survivors that may only shrink** -- moves the upkeep into a file: a correct new function can add equivalents, and a refactor changes which mutant is which. Rejected.

Reason to revisit -- several runs over one critical area turn up mostly confirmed gaps, little repeated noise, and fit an agreed budget; a narrow gate with explicit exclusions for that area would then pay for itself.
