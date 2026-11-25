"""GraphQL input that graphql-core rejects, by the stage that finds it.

A schema id names the stage -- `schema=syntax` for a document that does not
parse, `schema=sdl` for one graphql-core builds no schema from, `schema=type-system`
for a built schema it finds invalid -- and the defect. A statement id names the
stage too: `statement=syntax` with the token the parser stops on, and
`statement=validation` with the validation rule that rejects it and the form it
rejects. What the diagnosis says comes from graphql-core itself, not from the
cases. Statements are written against `type Query { ping: String }`.

An empty selection is valid GraphQL that the generator rejects with the
diagnosis graphql-core gives when it is told not to accept one; an id names
what selects nothing. Every case is one line long and holds one empty selection.

Statements holding several operations parse and are rejected before
validation; their ids name how many operations and fragments they hold.
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

EMPTY_SELECTIONS: tuple[RejectedInput, ...] = (
    RejectedInput(id="empty-selection/of=field", text="query Find { user { } }"),
    RejectedInput(id="empty-selection/of=operation", text="query Find { }"),
    RejectedInput(id="empty-selection/of=anonymous-operation", text="{ }"),
    RejectedInput(id="empty-selection/of=subscription", text="subscription Watch { }"),
    RejectedInput(
        id="empty-selection/of=inline-fragment",
        text="query Find { user { ... on User { } } }",
    ),
    RejectedInput(
        id="empty-selection/of=inline-fragment-without-type",
        text="query Find { user { ... { } } }",
    ),
    RejectedInput(id="empty-selection/of=fragment", text="fragment Fields on User { }"),
    RejectedInput(id="empty-selection/of=slot", text="query Find { user @slot { } }"),
)

INVALID_STATEMENTS: tuple[RejectedInput, ...] = (
    RejectedInput(
        id="statement=validation/rule=known-operation-types/root=mutation",
        text="mutation Change { ping }",
    ),
    RejectedInput(
        id="statement=validation/rule=known-operation-types/root=subscription",
        text="subscription Watch { ping }",
    ),
    RejectedInput(
        id="statement=validation/rule=known-type-names/variable=bare",
        text="query Find($id: Missing) { ping }",
    ),
    RejectedInput(
        id="statement=validation/rule=known-type-names/variable=list-non-null",
        text="query Find($ids: [Missing!]!) { ping }",
    ),
    RejectedInput(
        id="statement=validation/rule=known-type-names/fragment-on=unknown",
        text="fragment Fields on Missing { ping }",
    ),
    RejectedInput(
        id="statement=validation/rule=fragments-on-composite-types/fragment-on=scalar",
        text="fragment Fields on String { ping }",
    ),
    RejectedInput(
        id="statement=validation/rule=known-fragment-names/spread-in=operation",
        text="query Find { ...Missing }",
    ),
    RejectedInput(
        id="statement=validation/rule=fields-on-correct-type/fragments=2-unused",
        text="fragment Left on Query { ping }\nfragment Right on Query { absent }",
    ),
    RejectedInput(
        id="statement=validation/rule=no-fragment-cycles/cycle=self",
        text="fragment Fields on Query { ping ...Fields }",
    ),
    RejectedInput(
        id="statement=validation/rule=no-fragment-cycles/cycle=pair",
        text=(
            "fragment Left on Query { ping ...Right }\n"
            "fragment Right on Query { ping ...Left }"
        ),
    ),
)

SEVERAL_OPERATION_STATEMENTS: tuple[RejectedInput, ...] = (
    RejectedInput(
        id="operations=2/named=2/fragments=0",
        text="query First { ping }\nquery Second { ping }",
    ),
    RejectedInput(
        id="operations=2/named=1/fragments=0",
        text="{ ping }\nquery Second { ping }",
    ),
    RejectedInput(
        id="operations=2/named=2/fragments=1",
        text=(
            "query First { ...Fields }\n"
            "query Second { ping }\n"
            "fragment Fields on Query { ping }"
        ),
    ),
    RejectedInput(
        id="operations=3/named=3/fragments=0/roots=query+mutation",
        text="query First { ping }\nquery Second { ping }\nmutation Third { ping }",
    ),
)
