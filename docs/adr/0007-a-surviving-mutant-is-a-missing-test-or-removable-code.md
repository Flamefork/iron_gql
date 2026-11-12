# A surviving mutant is a missing test or removable code; only invariants and proven equivalents are excluded

`just mutants` fails while any mutant is left unkilled, so every survivor needs a fate, and the fate follows the coverage rule this project already holds: an uncovered branch is a missing test or dead defensive code. A surviving mutant is first a missing test: the test pins the behaviour the mutant changed, through the public generator and runtime like any other test. Text and keys of diagnostics count as behaviour: the place, diagnosis and hint of an error are promised, and a key such as `"message"` is part of the protocol shape, so tests compare the full error text rather than a fragment that a changed case or word slips past. When no test can tell the mutant from the original, the mutant is equivalent, and the code it touched is first simplified: an argument, branch or call whose removal leaves the behaviour the same is removed. Two cases are excluded from mutation instead. A `case _:` / `raise AssertionError` invariant, unreachable by construction, takes the same exemption as in coverage, through one mutmut mechanism with a comment naming it as that exemption. An equivalent mutant that no simplification removes takes a line `# pragma: no mutate` with a comment giving the reason, the way a narrow `# pyright: ignore[rule]` closes a hole in third-party stubs. No other exclusion: not by file, not by pattern over a kind of literal.

## Considered Options

- **Rewrite invariants so no defensive branch is left** (`assert_never`) — the coverage rule already accepts these branches, and mutation testing would ask for a second form of the same guarantee. Rejected.
- **Exclude the wording of diagnostics, test only keys and data shape** — the wording is the promised diagnosis and hint; excluding it leaves the promise without a check. Rejected.
- **A pragma on every equivalent mutant, code untouched** — an equivalent mutant often marks code that does nothing, and the pragma would keep it. Rejected.
- **No exclusions at all** — invariants unreachable by construction and equivalents of necessary code cannot be killed by any test. Rejected.

Reason to revisit — mutmut gains its own notion of skipped or equivalent mutants, or a kind of survivor turns up that none of these fates fits.
