"""Input types whose generated alias keeps the GraphQL type's name.

A `@oneOf` input is generated as an alias over its members, and the rename
pass that names result shapes (ADR 0003) never reaches it: the alias keeps
the input type's name, whatever result aliases over other types are called.
"""

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class InputName:
    id: str
    schema: str
    statement: str
    # The alias the module exports for the input.
    alias: str


INPUT_NAMES: tuple[InputName, ...] = (
    InputName(
        id="input=oneof/variable=non-null",
        schema="""
        type Query {
            find(filter: MediaFilter!): Post
        }

        type Post {
            id: ID!
        }

        input MediaFilter @oneOf {
            photo: ID
            video: ID
        }
        """,
        statement="query Find($filter: MediaFilter!) { find(filter: $filter) { id } }",
        alias="MediaFilter",
    ),
)
