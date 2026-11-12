"""Selections whose generated model names would coincide.

A detailed model name is a lossy spelling of the shape: tokens concatenate
without separators and lose underscores, a nullable marker and a list marker
are words a schema can spell too, a fixed artifact can be named anything, and
a fallback's `__typename` literal spells nothing unless it is given a token.
Each case is one way two spellings meet -- its id says which -- and once
failed generation or would have merged two shapes. Every shape has to survive
as a class of its own, reached from its own query.
"""

from dataclasses import dataclass
from dataclasses import field


@dataclass(frozen=True, kw_only=True)
class Payload:
    query: str
    # The `data` of a response to the query.
    data: dict[str, object]
    accepted: bool


@dataclass(frozen=True, kw_only=True)
class NameCollision:
    name: str
    schema: str
    queries: str
    # (query variable, field path from the result) -> the fields the model
    # there has; every path of a case must reach a different class.
    shapes: dict[tuple[str, tuple[str, ...]], frozenset[str]] = field(
        default_factory=dict
    )
    # Responses that tell the shapes' field types apart, which a check of the
    # field names alone cannot.
    payloads: tuple[Payload, ...] = ()


UNDERSCORE_SCHEMA = """
type Query {
    t: T
    p: P
}

type P {
    t: T
}

type T {
    a_b: String
    c: String
    a: String
    b_c: String
}
"""

NAME_COLLISIONS: tuple[NameCollision, ...] = (
    NameCollision(
        name="underscores-spell-one-field-suffix",
        schema=UNDERSCORE_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        q1 = api_gql("query Q1 { t { a_b c } }")
        q2 = api_gql("query Q2 { t { a b_c } }")
        """,
        shapes={
            ("q1", ("t",)): frozenset({"a_b", "c"}),
            ("q2", ("t",)): frozenset({"a", "b_c"}),
        },
    ),
    NameCollision(
        name="parent-over-two-colliding-children",
        schema=UNDERSCORE_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        q1 = api_gql("query Q1 { p { t { a_b c } } }")
        q2 = api_gql("query Q2 { p { t { a b_c } } }")
        """,
        shapes={
            ("q1", ("p",)): frozenset({"t"}),
            ("q2", ("p",)): frozenset({"t"}),
            ("q1", ("p", "t")): frozenset({"a_b", "c"}),
            ("q2", ("p", "t")): frozenset({"a", "b_c"}),
        },
    ),
    NameCollision(
        name="list-of-x-spells-type-list-x",
        schema="""
        type Query {
            t: T
        }

        type T {
            xs: [X!]!
            listX: ListX!
        }

        type X {
            id: ID!
        }

        type ListX {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q1 = api_gql("query Q1 { t { f: xs { id } } }")
        q2 = api_gql("query Q2 { t { f: listX { id } } }")
        """,
        shapes={
            ("q1", ("t",)): frozenset({"f"}),
            ("q2", ("t",)): frozenset({"f"}),
        },
        payloads=(
            Payload(query="q1", data={"t": {"f": [{"id": "1"}]}}, accepted=True),
            Payload(query="q1", data={"t": {"f": {"id": "1"}}}, accepted=False),
            Payload(query="q2", data={"t": {"f": {"id": "1"}}}, accepted=True),
            Payload(query="q2", data={"t": {"f": [{"id": "1"}]}}, accepted=False),
        ),
    ),
    NameCollision(
        name="nullable-foo-spells-type-opt-foo",
        schema="""
        type Query {
            t: T
        }

        type T {
            foo: Foo
            optFoo: OptFoo!
        }

        type Foo {
            id: ID!
        }

        type OptFoo {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q1 = api_gql("query Q1 { t { f: foo { id } } }")
        q2 = api_gql("query Q2 { t { f: optFoo { id } } }")
        """,
        shapes={
            ("q1", ("t",)): frozenset({"f"}),
            ("q2", ("t",)): frozenset({"f"}),
        },
        payloads=(
            Payload(query="q1", data={"t": {"f": None}}, accepted=True),
            Payload(query="q2", data={"t": {"f": None}}, accepted=False),
        ),
    ),
    # Each fallback stands for the `Node` types its query did not spell out;
    # the two shapes differ only in that `__typename` literal.
    NameCollision(
        name="fallbacks-differ-only-in-typename-literal",
        schema="""
        interface Node {
            id: ID!
        }

        type Post implements Node {
            id: ID!
            title: String!
        }

        type Comment implements Node {
            id: ID!
            body: String!
        }

        type User implements Node {
            id: ID!
        }

        type Query {
            node(id: ID!): Node
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q1 = api_gql(
            "query Q1 { node(id: 1) { __typename ... on Post { title } } }"
        )
        q2 = api_gql(
            "query Q2 { node(id: 1) { __typename ... on Comment { body } } }"
        )
        """,
        # The fallback of each query accepts the types the other spells out.
        payloads=tuple(
            Payload(query=query, data={"node": {"__typename": typename}}, accepted=True)
            for query, typename in (
                ("q1", "Comment"),
                ("q1", "User"),
                ("q2", "Post"),
                ("q2", "User"),
            )
        ),
    ),
    # A detailed name spells nested shapes one level deep (ADR 0003): the two
    # `Post` shapes share `PostWithAuthor_Opt_UserWithPosts`, and what tells
    # them apart lies two levels down.
    NameCollision(
        name="shapes-differ-below-the-one-level-candidate",
        schema="""
        type Query {
            feed: [Post!]!
        }

        type Post {
            id: ID!
            title: String
            author: User
        }

        type User {
            posts: [Post!]!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q1 = api_gql("query Q1 { feed { author { posts { id } } } }")
        q2 = api_gql("query Q2 { feed { author { posts { title } } } }")
        """,
        shapes={
            ("q1", ("feed",)): frozenset({"author"}),
            ("q2", ("feed",)): frozenset({"author"}),
            ("q1", ("feed", "author")): frozenset({"posts"}),
            ("q2", ("feed", "author")): frozenset({"posts"}),
        },
    ),
)
