"""Packages that define one fragment name differently in several places.

Written against `type Query { user: User }` and `type User { id: ID!, name:
String }`. An id names the statements in order by the kind that holds each
definition -- `single` for a statement of one fragment, `several` for one of
several fragment definitions, `operation` for an operation and its own
fragments, `operation-only` for an operation that only spreads -- and, after a
`/`, what else the case varies. `conflicts` names each fragment defined
differently and the statements, by index, that define it; `invalid` names the
statements graphql-core rejects on their own.
"""

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class FragmentNameConflict:
    id: str
    statements: tuple[str, ...]
    conflicts: tuple[tuple[str, tuple[int, ...]], ...]
    invalid: tuple[int, ...] = ()


FRAGMENT_NAME_CONFLICTS: tuple[FragmentNameConflict, ...] = (
    FragmentNameConflict(
        id="single+single",
        statements=(
            "fragment Fields on User { id }",
            "fragment Fields on User { name }",
        ),
        conflicts=(("Fields", (0, 1)),),
    ),
    FragmentNameConflict(
        id="single+several",
        statements=(
            "fragment Fields on User { id }",
            "fragment Fields on User { name }\nfragment Other on User { id }",
        ),
        conflicts=(("Fields", (0, 1)),),
    ),
    FragmentNameConflict(
        id="several+single",
        statements=(
            "fragment Fields on User { name }\nfragment Other on User { id }",
            "fragment Fields on User { id }",
        ),
        conflicts=(("Fields", (0, 1)),),
    ),
    FragmentNameConflict(
        id="several+several",
        statements=(
            "fragment Fields on User { id }\nfragment One on User { id }",
            "fragment Fields on User { name }\nfragment Two on User { id }",
        ),
        conflicts=(("Fields", (0, 1)),),
    ),
    FragmentNameConflict(
        id="operation+single",
        statements=(
            "query Find { user { ...Fields } }\nfragment Fields on User { id }",
            "fragment Fields on User { name }",
        ),
        conflicts=(("Fields", (0, 1)),),
    ),
    FragmentNameConflict(
        id="operation+single+operation-only",
        statements=(
            "fragment Fields on User { name }\nquery UseOne { user { id ...Fields } }",
            "fragment Fields on User { id }",
            "query UseOther { user { ...Fields } }",
        ),
        conflicts=(("Fields", (0, 1)),),
    ),
    FragmentNameConflict(
        id="single+single+single/two-agree",
        statements=(
            "fragment Fields on User { id }",
            "fragment Fields on User { name }",
            "fragment Fields on User { id }",
        ),
        conflicts=(("Fields", (0, 1, 2)),),
    ),
    FragmentNameConflict(
        id="several+single/twice-in-one-statement",
        statements=(
            "fragment Fields on User { id }\nfragment Fields on User { name }",
            "fragment Fields on User { name }",
        ),
        conflicts=(("Fields", (0, 1)),),
        invalid=(0,),
    ),
    FragmentNameConflict(
        id="operation+several+single/two-names",
        statements=(
            "query Find { user { ...Bits } }\nfragment Bits on User { id }",
            "fragment Bits on User { ...Extra }\nfragment Extra on User { id }",
            "fragment Extra on User { __typename }",
        ),
        conflicts=(("Bits", (0, 1)), ("Extra", (1, 2))),
    ),
)
