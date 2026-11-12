"""GraphQL input that graphql-core rejects, by the stage that finds it.

A schema id names the stage -- `schema=syntax` for a document that does not
parse, `schema=sdl` for one graphql-core builds no schema from, `schema=type-system`
for a built schema it finds invalid -- and the defect. A statement id names the
token the parser stops on. What the diagnosis says comes from graphql-core
itself, not from the cases.
"""

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class RejectedInput:
    id: str
    text: str


REJECTED_SCHEMAS: tuple[RejectedInput, ...] = (
    RejectedInput(
        id="schema=syntax/defect=unclosed-type", text="type Query {\n  ping: String\n"
    ),
    RejectedInput(
        id="schema=sdl/defect=unknown-type", text="type Query { ping: Missing }"
    ),
    RejectedInput(
        id="schema=sdl/defect=repeated-type",
        text="type Query { a: Int }\ntype Query { b: Int }",
    ),
    RejectedInput(
        id="schema=sdl/defect=unknown-type-twice",
        text="type Query { a: Missing, b: Absent }",
    ),
    RejectedInput(
        id="schema=type-system/defect=no-query-root", text="type Post { title: String }"
    ),
    RejectedInput(
        id="schema=type-system/defect=interface-field-missing",
        text=(
            "interface Node { id: ID }\n"
            "type Query { node: Node }\n"
            "type User implements Node { name: String }"
        ),
    ),
)

REJECTED_STATEMENTS: tuple[RejectedInput, ...] = (
    RejectedInput(id="statement=syntax/stop=eof", text="query Broken { ping "),
    RejectedInput(
        id="statement=syntax/stop=unexpected-token", text="query Broken { ping( }"
    ),
    RejectedInput(
        id="statement=syntax/stop=unterminated-string",
        text='query Broken { ping(a: "x) }',
    ),
    RejectedInput(
        id="statement=syntax/stop=fragment-without-type", text="fragment F { ping }"
    ),
)
