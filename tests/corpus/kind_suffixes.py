"""The kind suffix on every class the developer named (ADR 0004).

An operation or a template carries `Query`, `Mutation` or `Subscription`, a
fragment `Fragment` -- without exceptions: a name already ending in its suffix
gets it again, and so does an anonymous operation's `Anonymous<hash>`, whatever
its kind: the suffix already names the kind, and the base says where the hash
comes from. The classes named after one (its result, a template's bound base,
a fragment's data and a factory's applied class) follow it.

Every expected name is written out by hand. An anonymous operation's carries
its hash; the case spells that as `<hash>`, the form of the name without the
value.
"""

from dataclasses import dataclass

SCHEMA = """
type Query {
    post: Post
}

type Mutation {
    touch: Post
}

type Subscription {
    posted: Post
}

type Post {
    id: ID!
    media: Media
}

union Media = Photo | Video

type Photo {
    url: String!
    thumb(width: Int!): String
}

type Video {
    url: String!
}
"""

# A fragment is generated only where a slot can take it.
HOLDER = "query Holder { post { media @slot { __typename } } }"

HASH = "<hash>"


@dataclass(frozen=True, kw_only=True)
class KindSuffix:
    # Every axis: kind, class (operation, template, fragment, factory) and
    # the form of the written name.
    id: str
    statement: str
    # Statements the module needs beside it.
    context: tuple[str, ...] = ()
    # The name of the class `api_gql(statement)` returns.
    class_name: str
    # The names the module exports next to it.
    related: tuple[str, ...]
    # A factory's arguments, and the name of the applied class `with_args`
    # returns.
    args: dict[str, object] | None = None
    applied: str | None = None


KIND_SUFFIXES: tuple[KindSuffix, ...] = (
    KindSuffix(
        id="kind=query/class=operation/name=plain",
        statement="query GetPost { post { id } }",
        class_name="GetPostQuery",
        related=("GetPostQueryResult",),
    ),
    KindSuffix(
        id="kind=query/class=operation/name=suffixed",
        statement="query GetPostQuery { post { id } }",
        class_name="GetPostQueryQuery",
        related=("GetPostQueryQueryResult",),
    ),
    KindSuffix(
        # Spelled like the schema's type `Post`, which keeps its name.
        id="kind=query/class=operation/name=type-name",
        statement="query Post { post { id } }",
        class_name="PostQuery",
        related=("PostQueryResult",),
    ),
    KindSuffix(
        id="kind=query/class=operation/name=anonymous",
        statement="{ post { id } }",
        class_name="Anonymous<hash>Query",
        related=("Anonymous<hash>QueryResult",),
    ),
    KindSuffix(
        id="kind=query/class=template/name=plain",
        statement="query GetPost { post { media @slot { __typename } } }",
        class_name="GetPostQuery",
        related=("GetPostQueryResult", "GetPostQueryBound"),
    ),
    KindSuffix(
        id="kind=query/class=template/name=suffixed",
        statement="query GetPostQuery { post { media @slot { __typename } } }",
        class_name="GetPostQueryQuery",
        related=("GetPostQueryQueryResult", "GetPostQueryQueryBound"),
    ),
    KindSuffix(
        id="kind=query/class=template/name=anonymous",
        statement="{ post { media @slot { __typename } } }",
        class_name="Anonymous<hash>Query",
        related=("Anonymous<hash>QueryResult", "Anonymous<hash>QueryBound"),
    ),
    KindSuffix(
        id="kind=mutation/class=operation/name=plain",
        statement="mutation Touch { touch { id } }",
        class_name="TouchMutation",
        related=("TouchMutationResult",),
    ),
    KindSuffix(
        id="kind=mutation/class=operation/name=suffixed",
        statement="mutation TouchMutation { touch { id } }",
        class_name="TouchMutationMutation",
        related=("TouchMutationMutationResult",),
    ),
    KindSuffix(
        id="kind=mutation/class=operation/name=anonymous",
        statement="mutation { touch { id } }",
        class_name="Anonymous<hash>Mutation",
        related=("Anonymous<hash>MutationResult",),
    ),
    KindSuffix(
        id="kind=mutation/class=template/name=plain",
        statement="mutation Touch { touch { media @slot { __typename } } }",
        class_name="TouchMutation",
        related=("TouchMutationResult", "TouchMutationBound"),
    ),
    KindSuffix(
        id="kind=mutation/class=template/name=suffixed",
        statement="mutation TouchMutation { touch { media @slot { __typename } } }",
        class_name="TouchMutationMutation",
        related=("TouchMutationMutationResult", "TouchMutationMutationBound"),
    ),
    KindSuffix(
        id="kind=mutation/class=template/name=anonymous",
        statement="mutation { touch { media @slot { __typename } } }",
        class_name="Anonymous<hash>Mutation",
        related=("Anonymous<hash>MutationResult", "Anonymous<hash>MutationBound"),
    ),
    KindSuffix(
        id="kind=subscription/class=operation/name=plain",
        statement="subscription Posted { posted { id } }",
        class_name="PostedSubscription",
        related=("PostedSubscriptionResult",),
    ),
    KindSuffix(
        id="kind=subscription/class=operation/name=suffixed",
        statement="subscription PostedSubscription { posted { id } }",
        class_name="PostedSubscriptionSubscription",
        related=("PostedSubscriptionSubscriptionResult",),
    ),
    KindSuffix(
        id="kind=subscription/class=operation/name=anonymous",
        statement="subscription { posted { id } }",
        class_name="Anonymous<hash>Subscription",
        related=("Anonymous<hash>SubscriptionResult",),
    ),
    KindSuffix(
        id="kind=subscription/class=template/name=plain",
        statement="subscription Posted { posted { media @slot { __typename } } }",
        class_name="PostedSubscription",
        related=("PostedSubscriptionResult", "PostedSubscriptionBound"),
    ),
    KindSuffix(
        id="kind=subscription/class=template/name=suffixed",
        statement=(
            "subscription PostedSubscription { posted { media @slot { __typename } } }"
        ),
        class_name="PostedSubscriptionSubscription",
        related=(
            "PostedSubscriptionSubscriptionResult",
            "PostedSubscriptionSubscriptionBound",
        ),
    ),
    KindSuffix(
        id="kind=subscription/class=template/name=anonymous",
        statement="subscription { posted { media @slot { __typename } } }",
        class_name="Anonymous<hash>Subscription",
        related=(
            "Anonymous<hash>SubscriptionResult",
            "Anonymous<hash>SubscriptionBound",
        ),
    ),
    # A fragment has no anonymous form: GraphQL requires its name.
    KindSuffix(
        id="kind=fragment/class=fragment/name=plain",
        statement="fragment PhotoUrl on Photo { url }",
        context=(HOLDER,),
        class_name="PhotoUrlFragment",
        related=("PhotoUrlData",),
    ),
    KindSuffix(
        id="kind=fragment/class=fragment/name=suffixed",
        statement="fragment PhotoUrlFragment on Photo { url }",
        context=(HOLDER,),
        class_name="PhotoUrlFragmentFragment",
        related=("PhotoUrlFragmentData",),
    ),
    KindSuffix(
        id="kind=fragment/class=factory/name=plain",
        statement="fragment Thumb on Photo { thumb(width: $width) }",
        context=(HOLDER,),
        class_name="ThumbFragment",
        related=("ThumbData",),
        args={"width": 1},
        applied="_ThumbFragmentApplied",
    ),
    KindSuffix(
        id="kind=fragment/class=factory/name=suffixed",
        statement="fragment ThumbFragment on Photo { thumb(width: $width) }",
        context=(HOLDER,),
        class_name="ThumbFragmentFragment",
        related=("ThumbFragmentData",),
        args={"width": 1},
        applied="_ThumbFragmentFragmentApplied",
    ),
)
