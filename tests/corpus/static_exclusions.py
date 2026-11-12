"""Selections that @skip/@include conditions exclude statically.

A condition that can never hold -- a literal `@include(if: false)` or
`@skip(if: true)`, or `@include(if: $a)` and `@skip(if: $a)` on one variable,
together on a node or split between a node and its ancestor -- drops what it
covers from collection. Two diagnoses follow from it: a template slot whose
field never survives (`excluded=slot`), and a selection set left with no field
at all (`excluded=selection`). A condition on the slot field itself has its own
rejection and is not a case here.

An id names what is excluded, the condition (`include-false`, `skip-true`,
`variable-pair`), and where it stands: on the excluded field, on an inline
fragment or a fragment spread around it, on its parent field, or split
between the parent field and the node below. A slot field lives only in an
operation, so no fragment spread stands above it. `target` is the slot name
or the model the empty selection would have produced.
"""

from dataclasses import dataclass
from typing import Literal

SCHEMA = """
type Query {
    ping: String
    post(id: ID!): Post
}

type Post {
    id: ID!
    title: String
    author: User
}

type User {
    id: ID!
    name: String
}
"""


@dataclass(frozen=True, kw_only=True)
class StaticExclusion:
    id: str
    excluded: Literal["slot", "selection"]
    operation: str
    target: str
    text: str


def _slot(case_id: str, text: str) -> StaticExclusion:
    return StaticExclusion(
        id=f"excluded=slot/{case_id}",
        excluded="slot",
        operation="GetPost",
        target="author",
        text=text,
    )


def _selection(case_id: str, target: str, text: str) -> StaticExclusion:
    return StaticExclusion(
        id=f"excluded=selection/{case_id}",
        excluded="selection",
        operation="GetPost",
        target=target,
        text=text,
    )


STATIC_EXCLUSIONS: tuple[StaticExclusion, ...] = (
    _slot(
        "condition=include-false/on=inline-fragment",
        """
        query GetPost($id: ID!) {
            post(id: $id) { id ... @include(if: false) { author @slot { __typename } } }
        }
        """,
    ),
    _slot(
        "condition=skip-true/on=inline-fragment",
        """
        query GetPost($id: ID!) {
            post(id: $id) { id ... @skip(if: true) { author @slot { __typename } } }
        }
        """,
    ),
    _slot(
        "condition=variable-pair/on=inline-fragment",
        """
        query GetPost($id: ID!, $a: Boolean!) {
            post(id: $id) {
                id
                ... @include(if: $a) @skip(if: $a) { author @slot { __typename } }
            }
        }
        """,
    ),
    _slot(
        "condition=include-false/on=parent-field",
        """
        query GetPost($id: ID!) {
            ping
            post(id: $id) @include(if: false) { author @slot { __typename } }
        }
        """,
    ),
    _slot(
        "condition=skip-true/on=parent-field",
        """
        query GetPost($id: ID!) {
            ping
            post(id: $id) @skip(if: true) { author @slot { __typename } }
        }
        """,
    ),
    _slot(
        "condition=variable-pair/on=parent-field-and-inline-fragment",
        """
        query GetPost($id: ID!, $a: Boolean!) {
            post(id: $id) @include(if: $a) {
                id
                ... @skip(if: $a) { author @slot { __typename } }
            }
        }
        """,
    ),
    _selection(
        "condition=include-false/on=field",
        "GetPostQueryResultPost",
        """
        query GetPost($id: ID!) {
            ping
            post(id: $id) { title @include(if: false) }
        }
        """,
    ),
    _selection(
        "condition=skip-true/on=field",
        "GetPostQueryResultPost",
        """
        query GetPost($id: ID!) {
            ping
            post(id: $id) { title @skip(if: true) }
        }
        """,
    ),
    _selection(
        "condition=variable-pair/on=field",
        "GetPostQueryResultPost",
        """
        query GetPost($id: ID!, $a: Boolean!) {
            ping
            post(id: $id) { title @include(if: $a) @skip(if: $a) }
        }
        """,
    ),
    _selection(
        "condition=include-false/on=inline-fragment",
        "GetPostQueryResultPost",
        """
        query GetPost($id: ID!) {
            ping
            post(id: $id) { ... @include(if: false) { title } }
        }
        """,
    ),
    _selection(
        "condition=include-false/on=fragment-spread",
        "GetPostQueryResultPost",
        """
        query GetPost($id: ID!) {
            ping
            post(id: $id) { ...PostTitle @include(if: false) }
        }

        fragment PostTitle on Post { title }
        """,
    ),
    _selection(
        "condition=variable-pair/on=parent-field-and-field",
        "GetPostQueryResultPost",
        """
        query GetPost($id: ID!, $a: Boolean!) {
            ping
            post(id: $id) @include(if: $a) { title @skip(if: $a) }
        }
        """,
    ),
    _selection(
        "condition=include-false/on=parent-field",
        "GetPostQueryResult",
        """
        query GetPost($id: ID!) {
            post(id: $id) @include(if: false) { title }
        }
        """,
    ),
)
