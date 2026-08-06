import inspect
import json
import weakref
from pathlib import Path

import pydantic
import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer
from werkzeug import Request
from werkzeug import Response

from iron_gql.codegen import GraphQLGenerationError
from iron_gql.runtime import ASGIApp
from iron_gql.runtime import ASGIReceive
from iron_gql.runtime import ASGIScope
from iron_gql.runtime import ASGISend
from iron_gql.testing import accept_graphql_ws
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.conftest import gql_server
from tests.conftest import use_package_client

SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
    caption: String!
}

type LinkAttachment {
    href: String!
}
"""

NESTED_SCHEMA = """
type Query {
    board(id: ID!): Board
}

type Board {
    id: ID!
    owner: Owner
    cards: [Card!]!
    activity: Activity
    events: [Activity!]!
}

type Owner {
    id: ID!
    fullName: String!
}

type Card {
    title: String!
}

union Activity = Comment | Move

type Comment {
    body: String!
    author: Owner!
}

type Move {
    fromColumn: String!
    author: Owner!
}
"""

generated_package(
    "slots_basic",
    schema=SCHEMA
    + """
type Mutation {
    attach(id: ID!): Post
}
""",
    queries='''
    from tests.generated.slots_basic.gql.api import api_gql

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )

    attach = api_gql(
        """
        mutation Attach($id: ID!) {
            attach(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )
    ''',
)

generated_package(
    "slots_isolation",
    schema=NESTED_SCHEMA,
    queries='''
    from tests.generated.slots_isolation.gql.api import api_gql

    get_board = api_gql(
        """
        query GetBoard($id: ID!) {
            board(id: $id) @slot {
                __typename
                owner { who: fullName }
                cards { title }
                activity {
                    __typename
                    ... on Comment { body author { who: fullName } }
                    ... on Move { fromColumn author { id } }
                }
            }
        }
        """
    )

    ping_board = api_gql(
        """
        query PingBoard($id: ID!) {
            board(id: $id) @slot { __typename }
        }
        """
    )

    ping_main = api_gql(
        """
        query PingMain($id: ID!) {
            main: board(id: $id) @slot { __typename }
        }
        """
    )

    merged_board = api_gql(
        """
        query MergedBoard($id: ID!) {
            merged: board(id: $id) { __typename }
            merged: board(id: $id) @slot { __typename }
        }
        """
    )
    ''',
)

generated_package(
    "slots_lists",
    schema=NESTED_SCHEMA,
    queries='''
    from tests.generated.slots_lists.gql.api import api_gql

    get_events = api_gql(
        """
        query GetEvents($id: ID!) {
            board(id: $id) @slot {
                __typename
                events { __typename }
            }
        }
        """
    )

    get_cards = api_gql(
        """
        query GetCards($id: ID!) {
            board(id: $id) {
                cards @slot { __typename }
            }
        }
        """
    )

    activity_texts = api_gql(
        """
        fragment ActivityTexts on Board {
            events {
                __typename
                ... on Comment { body }
                ... on Move { fromColumn }
            }
        }
        """
    )

    card_title = api_gql(
        """
        fragment CardTitle on Card {
            title
        }
        """
    )
    ''',
)


SCHEMA_MULTI = """
type Query {
    posts: [Post!]!
}

type Post {
    id: ID!
    attachment: Attachment
    preview: Previewable
    owner: Owner!
}

union Attachment = ImageAttachment | LinkAttachment

# Intersects Attachment on ImageAttachment without being equal to it, so a
# fragment on ImageAttachment is spread-compatible with both slot types at once.
interface Previewable {
    url: String!
}

type ImageAttachment implements Previewable {
    url: String!
    album: Album!
}

type LinkAttachment {
    href: String!
}

type Album {
    id: ID!
    title: String!
    cover: String!
}

interface Owner {
    id: ID!
}

type UserOwner implements Owner {
    id: ID!
    email: String!
}

type TeamOwner implements Owner {
    id: ID!
    memberCount: Int!
}
"""

generated_package(
    "slots_execute",
    schema=SCHEMA,
    queries='''
    from tests.generated.slots_execute.gql.api import api_gql

    image_url = api_gql(
        """
        fragment ImageUrl on ImageAttachment {
            url
        }
        """
    )

    image_caption = api_gql(
        """
        fragment ImageCaption on ImageAttachment {
            caption
        }
        """
    )

    link_href = api_gql(
        """
        fragment LinkHref on LinkAttachment {
            href
        }
        """
    )

    attachment_identity = api_gql(
        """
        fragment AttachmentIdentity on Attachment {
            __typename
            ... on ImageAttachment { caption }
            ... on LinkAttachment { href }
        }
        """
    )

    get_attachment = api_gql(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )
    ''',
)


generated_package(
    "slots_multi",
    schema=SCHEMA_MULTI,
    queries='''
    from tests.generated.slots_multi.gql.api import api_gql

    album_title = api_gql(
        """
        fragment AlbumTitle on ImageAttachment {
            album { title }
        }
        """
    )

    album_cover = api_gql(
        """
        fragment AlbumCover on ImageAttachment {
            album { cover }
        }
        """
    )

    owner_identity = api_gql(
        """
        fragment OwnerIdentity on Owner {
            __typename
            id
            ... on UserOwner { email }
            ... on TeamOwner { memberCount }
        }
        """
    )

    album_summary = api_gql(
        """
        fragment AlbumSummary on Album {
            id
        }
        """
    )

    list_posts = api_gql(
        """
        query ListPosts {
            posts {
                id
                attachment @slot { __typename }
                preview @slot { __typename }
                owner @slot { __typename }
            }
        }
        """
    )
    ''',
)


SCHEMA_SUBSCRIPTION = (
    SCHEMA
    + """
type Subscription {
    attachmentChanged(id: ID!): Post!
}
"""
)

generated_package(
    "slots_subscription",
    schema=SCHEMA_SUBSCRIPTION,
    queries='''
    from tests.generated.slots_subscription.gql.api import api_gql

    image_url = api_gql(
        """
        fragment ImageUrl on ImageAttachment {
            url
        }
        """
    )

    watch_attachment = api_gql(
        """
        subscription WatchAttachment($id: ID!) {
            attachmentChanged(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
        """
    )
    ''',
)


from tests.generated.slots_basic import queries as basic_queries
from tests.generated.slots_basic.gql import api as basic_api
from tests.generated.slots_execute import queries as execute_queries
from tests.generated.slots_execute.gql.api import AttachmentIdentityDataImageAttachment
from tests.generated.slots_execute.gql.api import AttachmentIdentityDataLinkAttachment
from tests.generated.slots_isolation import queries as isolation_queries
from tests.generated.slots_isolation.gql.api import GetBoardResult
from tests.generated.slots_isolation.gql.api import GetBoardResultBoardSlot
from tests.generated.slots_isolation.gql.api import (
    GetBoardResultBoardSlotActivityComment,
)
from tests.generated.slots_isolation.gql.api import (
    GetBoardResultBoardSlotActivityCommentAuthor,
)
from tests.generated.slots_lists import queries as lists_queries
from tests.generated.slots_lists.gql.api import ActivityTextsDataEventsComment
from tests.generated.slots_lists.gql.api import ActivityTextsDataEventsMove
from tests.generated.slots_multi import queries as multi_queries
from tests.generated.slots_multi.gql import api as multi_api
from tests.generated.slots_multi.gql.api import OwnerIdentityDataTeamOwner
from tests.generated.slots_multi.gql.api import OwnerIdentityDataUserOwner
from tests.generated.slots_subscription import queries as subscription_queries


def generated_source(package: str) -> str:
    return (Path(__file__).parent / "generated" / package / "gql" / "api.py").read_text(
        encoding="utf-8"
    )


def test_slot_directive_is_stripped_and_split_in_exec_source():
    generated = generated_source("slots_basic")
    # `@slot` legitimately survives elsewhere in the file: `api_gql`'s overload
    # literal and dispatch dict key are the developer's exact source text, used
    # to resolve the call to its typed return value. Only the exec source —
    # the parts actually sent to the server, embedded in `execute()` — must be
    # free of the directive and split at the slot's position.
    exec_source_lines = [
        line for line in generated.splitlines() if "build_slot_source" in line
    ]
    assert exec_source_lines, "build_slot_source call not found in generated api.py"
    assert all("@slot" not in line for line in exec_source_lines)
    assert all("('attachment', " in line for line in exec_source_lines)


def test_slot_inside_fragment_is_rejected(test_project: ProjectBuilder):
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        post_fields = api_gql(
            '''
            fragment PostFields on Post {
                attachment @slot { __typename }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="@slot is only allowed"):
        test_project.generate()


def test_slot_without_typename_is_rejected(test_project: ProjectBuilder):
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot { __typename @skip(if: true) }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="__typename"):
        test_project.generate()


def test_slot_on_a_scalar_field_reports_the_scalar(test_project: ProjectBuilder):
    # A scalar field takes no selection set at all, so the __typename rule has
    # nothing it could ever be satisfied by here: checked in that order, the
    # error would demand a selection the language forbids.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    id @slot
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="not on a composite field"):
        test_project.generate()


def test_slot_name_colliding_with_variable_is_rejected(test_project: ProjectBuilder):
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($attachment: ID!) {
                post(id: $attachment) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Execute parameter 'attachment'"):
        test_project.generate()


def test_slot_name_covering_different_types_is_rejected(test_project: ProjectBuilder):
    # Same alias `x` on two @slot fields that resolve to different composite
    # types: `post` itself (Post) and, nested under a second,
    # separately-aliased call to `post`, `attachment` (Attachment).
    # Both slots use the same alias but aren't siblings in the same selection
    # set, so this is valid GraphQL on its own (no alias-merge conflict) —
    # only the slot rule should reject it.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                x: post(id: $id) @slot { __typename }
                y: post(id: $id) {
                    x: attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="different types"):
        test_project.generate()


def test_slot_kwarg_colliding_with_a_variable_by_case_is_rejected(
    test_project: ProjectBuilder,
):
    # `$ImageUrl` and the slot alias `imageUrl` are different GraphQL names,
    # so the exact-name rule sees nothing — but both snake to `image_url` and
    # `execute` would be rendered with the same kwarg twice. That source parses
    # fine and only fails at `compile()`, so nothing downstream catches it.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($ImageUrl: ID!) {
                post(id: $ImageUrl) {
                    imageUrl: attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Execute parameter 'image_url'"):
        test_project.generate()


def test_variable_snaking_to_slot_fragments_is_rejected(test_project: ProjectBuilder):
    # The rendered `execute` binds a `slot_fragments` local before building
    # `variables`, so a variable snaking to that name would silently send the
    # mapping to the server instead of the caller's value.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($slotFragments: ID!) {
                post(id: $slotFragments) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match="Execute parameter 'slot_fragments'"
    ):
        test_project.generate()


def test_variable_named_slots_is_rejected(test_project: ProjectBuilder):
    # `execute` reads `slots.as_handles` and `slots.build_slot_source`, so a
    # parameter named `slots` would shadow the module inside the method body.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($slots: ID!) {
                post(id: $slots) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Execute parameter 'slots'"):
        test_project.generate()


PATH_COLLISION_SCHEMA = """
type Query {
    aB: Outer
    a: Inner
}

type Outer {
    c: Attachment2
}

type Inner {
    bC: Attachment2
    b: Inner2
}

type Inner2 {
    c: Attachment2
}

type Attachment2 {
    id: ID!
    name: String
}
"""


def test_slot_paths_colliding_on_model_name_are_rejected(
    test_project: ProjectBuilder,
):
    # Slot model names concatenate the PascalCase path without separators, so
    # `aB.c` and `a.bC` both produce `...ABCSlot`. Slot models are excluded
    # from the rename pass, and merging them would leave one class whose
    # `slot_name__` can only name one of the two slots.
    test_project.prepare(
        schema=PATH_COLLISION_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_slots = api_gql(
            '''
            query GetSlots {
                aB { c @slot { __typename } }
                a { bC @slot { __typename } }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match=r"by slot 'c' and by slot 'bC'"):
        test_project.generate()


def test_same_slot_with_conflicting_selections_is_rejected(
    test_project: ProjectBuilder,
):
    # `aB.c` and `a.b.c` both name the model `...ABCSlot` and both carry the
    # slot name `c` — a single QuerySlot — but their static selections differ,
    # and one class cannot hold both shapes.
    test_project.prepare(
        schema=PATH_COLLISION_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_slots = api_gql(
            '''
            query GetSlots {
                aB { c @slot { __typename } }
                a { b { c @slot { __typename id } } }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match=r"Slot 'c' generates model .* from two"
    ):
        test_project.generate()


def test_slot_selection_aliasing_runtime_metadata_is_rejected(
    test_project: ProjectBuilder,
):
    # `slot_name__: url` is a legal alias, but the rendered field annotation
    # would shadow the ClassVar of the same name and pydantic would strip the
    # class attribute the runtime reads during validation.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot {
                        __typename
                        ... on ImageAttachment { slot_name__: url }
                    }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="slot runtime contract"):
        test_project.generate()


def test_base_url_symbol_shadowing_the_slots_module_is_rejected(
    test_project: ProjectBuilder,
):
    # `from iron_gql import slots` and the base URL import both bind `slots`;
    # collapsing scaffold claims into a set used to hide exactly this pair.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        slots = "http://testserver/graphql/"

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Name 'slots' is claimed by"):
        test_project.generate(base_url_import="sample_app.queries:slots")


def test_reserved_marker_token_in_operation_is_rejected(test_project: ProjectBuilder):
    # The exec source is split at synthesized `__slot__<i>__` tokens, each
    # verified to occur exactly once in the printed operation; user text
    # spelling out that exact token makes the split ambiguous and must be a
    # loud error rather than a silent mis-split.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment {
                post(id: "__slot__0__") {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(ValueError, match="reserved marker token"):
        test_project.generate()


def test_conditional_slot_is_rejected(test_project: ProjectBuilder):
    # A slot field is always requested: its spreads are spliced wherever the
    # node selects the key, and a caller that wants no fragment data passes an
    # empty list — so @skip/@include on the slot field itself is rejected
    # instead of producing a key that can arrive without its fragments.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($flag: Boolean!) {
                post(id: "1") {
                    attachment @slot @include(if: $flag) { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(ValueError, match="cannot carry @skip/@include"):
        test_project.generate()


def test_slot_conditional_through_mixed_polarity_variables_is_rejected(
    test_project: ProjectBuilder,
):
    # $b guards the marker branch via @include and the plain branch via @skip,
    # so no single variable assignment shows both branches at once — the
    # invariant has to be judged over the inherited conditions themselves. At
    # $a=true, $b=false the plain branch keeps the key while the marker is
    # gone.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($a: Boolean!, $b: Boolean!) {
                post(id: "1") {
                    ... @include(if: $b) {
                        attachment @slot { __typename }
                    }
                    ... @include(if: $a) @skip(if: $b) {
                        attachment { __typename }
                    }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(ValueError, match=r"at \$a=true, \$b=false"):
        test_project.generate()


def test_slot_under_conditional_merged_parent_is_rejected(
    test_project: ProjectBuilder,
):
    # The slot node itself is unconditional, but it sits inside one of two
    # merged `post` selections: at $a=false the other parent still delivers
    # the key, without the marker. The parent's condition is inherited by
    # everything under it.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($a: Boolean!) {
                post(id: "1") @include(if: $a) {
                    attachment @slot { __typename }
                }
                post(id: "1") {
                    attachment { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(ValueError, match="is conditional while response key"):
        test_project.generate()


def test_slot_conditional_alongside_matching_conditional_selection_generates(
    test_project: ProjectBuilder,
):
    # The twin of the rejections above: both branches share one condition, so
    # every state that keeps the key also keeps the marker.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($a: Boolean!) {
                post(id: "1") {
                    ... @include(if: $a) {
                        attachment @slot { __typename }
                    }
                    ... @include(if: $a) {
                        attachment { __typename }
                    }
                }
            }
            '''
        )
        """,
    )
    assert test_project.generate() is True


def test_statically_excluded_slot_is_rejected(test_project: ProjectBuilder):
    # A literal `@include(if: false)` on an enclosing selection drops the slot
    # field from the collected models, but the slot kwarg and the
    # `{Type}Fragment` base are derived from the AST — the operation would
    # demand a handle whose data can never arrive, and the rendered module
    # would reference the never-imported `slots` runtime.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    id
                    ... @include(if: false) {
                        attachment @slot { __typename }
                    }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="statically excluded"):
        test_project.generate()


def test_nested_slot_is_rejected(test_project: ProjectBuilder):
    # A nested slot entangles two composition points: the inner field is part
    # of the outer slot's static selection, so every fragment passed to the
    # outer overlaps the inner's payload and splice point.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_post = api_gql(
            '''
            query GetPost($id: ID!) {
                post(id: $id) @slot {
                    __typename
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="nested inside slot"):
        test_project.generate()


# Appended to a queries block, so it carries that block's indentation: the
# fixture dedents the whole string before writing it.
SLOT_OPERATION = """
        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """

PARAMETERISED_FRAGMENT_SCHEMA = """
type Query {
    user(id: ID!): User
}

type User {
    id: ID!
    posts(limit: Int!): [Post!]!
}

type Post {
    id: ID!
}
"""


def test_fragment_with_variable_is_rejected(test_project: ProjectBuilder):
    # The fragment is on a member of the `attachment` slot's union, so it is a
    # handle some slot kwarg accepts — the only case the rule speaks about.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        conditional = api_gql(
            '''
            fragment ConditionalUrl on ImageAttachment {
                url @include(if: $withUrl)
            }
            '''
        )
        """
        + SLOT_OPERATION,
    )
    with pytest.raises(GraphQLGenerationError, match="cannot reference variables"):
        test_project.generate()


def test_fragment_spreading_another_fragment_is_rejected(
    test_project: ProjectBuilder,
):
    # A handle travels to the server as its own text alone; a spread inside it
    # would have to ship a definition resolved through the global fragment
    # index. A fragment a slot can accept must therefore be self-contained.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        url_bits = api_gql(
            '''
            fragment UrlBits on ImageAttachment {
                url
            }
            '''
        )

        outer = api_gql(
            '''
            fragment OuterUrl on ImageAttachment {
                caption
                ...UrlBits
            }
            '''
        )
        """
        + SLOT_OPERATION,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=r"'OuterUrl'.*spreads 'UrlBits'.*self-contained",
    ):
        test_project.generate()


def test_slot_compatible_fragment_with_a_variable_is_rejected_when_only_spread(
    test_project: ProjectBuilder,
):
    # The rule follows compatibility, not usage. Nothing here passes the
    # fragment into a slot: it is spread by name into `GetOther`, which declares
    # `$withUrl`, and the slot lives in a different operation. It is still
    # rejected, because which handles reach a slot is a runtime fact codegen
    # cannot see — the alternative is an error that surfaces at the first
    # `execute` passing it. This is the subcase a codebase hits when it adds its
    # first `@slot` next to fragments that have always taken variables.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        conditional = api_gql(
            '''
            fragment ConditionalUrl on ImageAttachment {
                url @include(if: $withUrl)
            }
            '''
        )

        get_other = api_gql(
            '''
            query GetOther($id: ID!, $withUrl: Boolean!) {
                post(id: $id) {
                    attachment {
                        __typename
                        ... on ImageAttachment { ...ConditionalUrl }
                    }
                }
            }
            '''
        )
        """
        + SLOT_OPERATION,
    )
    with pytest.raises(GraphQLGenerationError, match="cannot reference variables"):
        test_project.generate()


def test_parameterised_fragment_generates_without_slots(test_project: ProjectBuilder):
    # A named fragment taking its arguments from the operation that spreads it
    # is what static fragments have always been for, and nothing here can reach
    # a slot: this package has none. The rule may not touch it.
    test_project.prepare(
        schema=PARAMETERISED_FRAGMENT_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        user_posts = api_gql(
            '''
            fragment UserPosts on User {
                posts(limit: $limit) { id }
            }
            '''
        )

        get_user = api_gql(
            '''
            query GetUser($id: ID!, $limit: Int!) {
                user(id: $id) {
                    id
                    ...UserPosts
                }
            }
            '''
        )
        """,
    )
    assert test_project.generate() is True


def test_fragment_with_a_variable_no_slot_accepts_is_generated(
    test_project: ProjectBuilder,
):
    # The package does have a slot, on Attachment; this fragment is on
    # Post, which shares no possible type with it, so it never becomes a
    # handle at all — its statement passes through untyped and the variables
    # rule does not apply to it. Pins that the rule follows spread
    # compatibility rather than the mere presence of a slot in the package.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        conditional = api_gql(
            '''
            fragment ConditionalId on Post {
                id @include(if: $withId)
            }
            '''
        )
        """
        + SLOT_OPERATION,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "ConditionalIdData" not in generated
    assert "_GQL_PASSTHROUGH" in generated


def test_fragment_conflicting_with_the_slot_selection_is_rejected(
    test_project: ProjectBuilder,
):
    # The slot's own static selection already binds `value` to url, so
    # this single fragment can never be spread into it.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        caption_as_value = api_gql(
            '''
            fragment CaptionAsValue on ImageAttachment {
                value: caption
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot {
                        __typename
                        ... on ImageAttachment { value: url }
                    }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Fields 'value' conflict"):
        test_project.generate()


def test_conflicting_fragment_pair_on_one_slot_is_rejected(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        url_as_is = api_gql(
            '''
            fragment UrlAsIs on ImageAttachment {
                value: url
            }
            '''
        )

        caption_as_value = api_gql(
            '''
            fragment CaptionAsValue on ImageAttachment {
                value: caption
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Fields 'value' conflict"):
        test_project.generate()


def test_compatible_fragments_that_merge_cleanly_are_accepted(
    test_project: ProjectBuilder,
):
    # The same two type conditions and the same slot as the pair above, but the
    # aliases no longer collide — the combination pass must stay quiet here, or
    # it would reject the feature's main use case.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        url_as_is = api_gql(
            '''
            fragment UrlAsIs on ImageAttachment {
                src: url
            }
            '''
        )

        caption_as_value = api_gql(
            '''
            fragment CaptionAsValue on ImageAttachment {
                value: caption
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    assert test_project.generate() is True


def test_handle_shadowing_an_operations_own_fragment_is_rejected(
    test_project: ProjectBuilder,
):
    # The operation already ships a `Url` definition and the runtime appends
    # the handle's verbatim, so the query as sent would define the name twice.
    # A locally shadowed fragment is only legal while nothing splices a second
    # definition of that name in next to it.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            fragment Url on ImageAttachment {
                url
            }

            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot { __typename }
                    other: attachment {
                        ... on ImageAttachment { ...Url }
                    }
                }
            }
            '''
        )

        url = api_gql(
            '''
            fragment Url on ImageAttachment {
                caption
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="cannot accept fragment 'Url'"):
        test_project.generate()


def test_handle_statically_spread_by_the_same_operation_is_rejected(
    test_project: ProjectBuilder,
):
    # Nothing is shadowed here: there is one global `Url`, the operation
    # spreads it by name in a branch that has nothing to do with the slot, and
    # the handle would ship that very same definition alongside. Byte-identical
    # or not, the assembled query declares the name twice — so the reach of the
    # rule is wider than the shadowing case and pinned here.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        url = api_gql(
            '''
            fragment Url on ImageAttachment {
                url
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot { __typename }
                    other: attachment {
                        ... on ImageAttachment { ...Url }
                    }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match="cannot be both spread into an operation by name",
    ):
        test_project.generate()


def test_nested_slot_in_a_merged_response_key_is_rejected(
    test_project: ProjectBuilder,
):
    # Neither field node is nested inside the other in the source: `@slot` sits
    # on the first `post` and the inner slot lives in the second. Collect
    # merges them into one response key, and the merged model is a slot node
    # whose subtree holds another one — exactly the shape the rule exists to
    # stop, and invisible to a rule that walks the AST node by node.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) @slot { __typename }
                post(id: $id) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="nested inside slot"):
        test_project.generate()


def test_fragment_named_like_the_client_singleton_is_rejected(
    test_project: ProjectBuilder,
):
    # `API_CLIENT` is bound by the scaffold before any fragment is rendered, so
    # the singleton silently rebinds it and every `execute` then resolves the
    # client to a fragment instance.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        client = api_gql(
            '''
            fragment apiClient on ImageAttachment {
                url
            }
            '''
        )
        """
        + SLOT_OPERATION,
    )
    with pytest.raises(GraphQLGenerationError, match="'API_CLIENT' is claimed by"):
        test_project.generate()


def test_schema_type_named_after_the_scaffold_keeps_the_model_apart(
    test_project: ProjectBuilder,
):
    # The scaffold binds `GQLModel` before any model is rendered, so promoting a
    # result model to its bare GraphQL type name has to yield here the same way
    # it yields to a fragment handle.
    test_project.prepare(
        schema="""
        type Query {
            entry: GQLModel
        }

        type GQLModel {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        get_entry = api_gql(
            '''
            query GetEntry {
                entry { id }
            }
            '''
        )
        """,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class GQLModel(pydantic.BaseModel):" in generated
    assert "class GQLModel(GQLModel):" not in generated


def test_enum_named_after_an_unconditional_import_is_rejected(
    test_project: ProjectBuilder,
):
    # `import pydantic` is emitted whatever the package's options are, and every
    # model refers to it. Reserving it through `to_camel_fn_full_name` would let
    # the claim vanish the moment that option points elsewhere, turning a
    # generation error into `AttributeError` at import of the generated module.
    test_project.prepare(
        schema="""
        type Query {
            entry: Entry
        }

        type Entry {
            kind: pydantic!
        }

        enum pydantic {
            FIRST
            SECOND
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        get_entry = api_gql(
            '''
            query GetEntry {
                entry { kind }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="'pydantic' is claimed by"):
        _ = test_project.generate(to_camel_fn_full_name="sample_app.casing:to_camel")


def test_an_invalid_operation_with_slots_reports_its_error_once(
    test_project: ProjectBuilder,
):
    # The combination pass re-validates a copy of the document once per slot,
    # so an operation that is already invalid would have its own error
    # reprinted once per slot and bury the one line that matters.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        first = api_gql('''fragment First on ImageAttachment { a: url }''')
        second = api_gql('''fragment Second on ImageAttachment { b: url }''')
        third = api_gql('''fragment Third on ImageAttachment { c: url }''')

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    nope
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    assert str(exc_info.value).count("Cannot query field 'nope'") == 1


def test_variable_named_self_is_rejected(test_project: ProjectBuilder):
    # `execute` already takes `self` positionally; a second one parses fine and
    # only `compile()` rejects it.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($self: ID!) {
                post(id: $self) { id }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Execute parameter 'self'"):
        test_project.generate()


def test_fragment_named_like_an_operation_is_rejected(test_project: ProjectBuilder):
    # The fragment section renders after the operations, so by the time the
    # dispatch dict is built the name points at the handle class and
    # `api_gql(query_text).execute()` raises AttributeError.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        url = api_gql(
            '''
            fragment GetAttachment on ImageAttachment {
                url
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) { id }
            }
            '''
        )

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                post(id: $id) { attachment @slot { __typename } }
            }
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="'GetAttachment' is claimed by"):
        test_project.generate()


def test_fragment_name_colliding_with_its_own_singleton_is_rejected(
    test_project: ProjectBuilder,
):
    # A one-letter name capitalizes and upper-snakes to the same identifier, so
    # `N = N()` overwrites the class with an instance of itself.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        url = api_gql(
            '''
            fragment N on ImageAttachment {
                url
            }
            '''
        )
        """
        + SLOT_OPERATION,
    )
    with pytest.raises(GraphQLGenerationError, match="'N' is claimed by"):
        test_project.generate()


def test_fragment_named_like_a_compatibility_base_is_rejected(
    test_project: ProjectBuilder,
):
    # `{Type}Fragment` bases occupy the module namespace too; the handle would
    # rebind the name its own base class is declared under.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        url = api_gql(
            '''
            fragment AttachmentFragment on ImageAttachment {
                url
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match="'AttachmentFragment' is claimed by"
    ):
        test_project.generate()


def test_fragment_named_after_a_schema_type_keeps_the_model_apart(
    test_project: ProjectBuilder,
):
    # The handle class name is public API and cannot move, so the promotion of
    # a result model to its bare GraphQL type name has to yield instead. Without
    # that, the module would declare `class ImageAttachment` twice.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        image_attachment = api_gql(
            '''
            fragment ImageAttachment on ImageAttachment {
                caption
            }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {
                post(id: $id) {
                    attachment @slot {
                        __typename
                        ... on ImageAttachment { url }
                        ... on LinkAttachment { href }
                    }
                }
            }
            '''
        )
        """,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert (
        "class ImageAttachment(AttachmentFragment[ImageAttachmentData]):" in generated
    )
    assert "class ImageAttachment(GQLModel):" not in generated


def test_slot_subtree_models_are_open_and_the_rest_stay_strict():
    # A slot payload carries every passed fragment's fields next to the static
    # selection, so every model validating inside the slot subtree ignores
    # extra keys — at every depth, pinned here for the deepest one — while
    # models outside the subtree keep the strict config.
    generated = generated_source("slots_isolation")
    assert "class GetBoardResultBoardSlotOwner(GQLOpenModel):" in generated
    assert (
        "class GetBoardResultBoardSlotActivityCommentAuthor(GQLOpenModel):" in generated
    )
    assert "class GetBoardResult(GQLModel):" in generated
    assert GetBoardResultBoardSlot.model_config.get("extra") == "ignore"
    assert GetBoardResult.model_config.get("extra") == "forbid"


def test_slots_with_equal_static_selections_are_not_deduplicated():
    # PingBoard, PingMain and MergedBoard all select the same `{ __typename }`
    # on the same type, so the name-dedup pass would collapse them into a
    # single class whose one `slot_name__` would then have to speak for three
    # slots. GetBoard's richer selection makes the fourth.
    generated = generated_source("slots_isolation")
    for name in (
        "GetBoardResultBoardSlot",
        "PingBoardResultBoardSlot",
        "PingMainResultMainSlot",
        "MergedBoardResultMergedSlot",
    ):
        assert f"class {name}(GQLSlotModel):" in generated


def test_slot_is_detected_on_any_node_of_a_merged_response_key():
    # `merged` comes from two field nodes and only the second carries `@slot`
    # (a shared fragment selecting the field plus `@slot` in the operation is
    # the realistic shape of this). Slot collection and exec-source stripping
    # both fire on any node with the directive, so the model must too —
    # otherwise the operation gets a slot kwarg and a marker in its source but
    # a plain model that cannot hold fragment data.
    generated = generated_source("slots_isolation")
    assert 'slot_name__: ClassVar[str] = "merged"' in generated


def test_slot_subtree_ignores_foreign_fields_at_every_depth():
    # The server returns the union of every consumer's selection inside a slot,
    # so a node in the slot subtree sees fields it never asked for. The open
    # config ignores those, while the models expose exactly their own
    # selection. An empty handle tuple stands in for the fragments that would
    # have selected `email`/`position`/`authorId`.
    result = GetBoardResult.model_validate(
        {
            "board": {
                "__typename": "Board",
                "id": "b1",
                "owner": {"who": "Alice", "email": "alice@example.com"},
                "cards": [{"title": "one", "position": 1}],
                "activity": {
                    "__typename": "Comment",
                    "body": "hi",
                    "authorId": "u1",
                    "author": {"who": "Alice", "email": "alice@example.com"},
                },
            }
        },
        context={"board": ()},
    )
    assert result.board is not None
    assert result.board.owner is not None
    assert result.board.owner.who == "Alice"
    assert [card.title for card in result.board.cards] == ["one"]
    assert result.board.activity == GetBoardResultBoardSlotActivityComment(
        typename__="Comment",
        body="hi",
        author=GetBoardResultBoardSlotActivityCommentAuthor(who="Alice"),
    )


def _resolve_post(attachment: dict[str, object] | None):
    def resolve(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, object]:
        return {"id": id, "attachment": attachment}

    return resolve


IMAGE_PAYLOAD: dict[str, object] = {
    "__typename": "ImageAttachment",
    "url": "https://cdn.example/pic.png",
    "caption": "A picture",
}


async def test_execute_reads_fragment_through_slot(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_execute",
        {"Query": {"post": _resolve_post(IMAGE_PAYLOAD)}},
    ):
        result = await execute_queries.get_attachment.execute(
            id="p-1", attachment=execute_queries.image_url
        )
        assert result.post is not None
        image = execute_queries.image_url.read(result.post.attachment)
        assert image is not None
        assert image.url == "https://cdn.example/pic.png"


async def test_several_fragments_share_one_slot(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_execute",
        {"Query": {"post": _resolve_post(IMAGE_PAYLOAD)}},
    ):
        result = await execute_queries.get_attachment.execute(
            id="p-1",
            attachment=[
                execute_queries.image_url,
                execute_queries.image_caption,
            ],
        )
        assert result.post is not None
        node = result.post.attachment
        image = execute_queries.image_url.read(node)
        caption = execute_queries.image_caption.read(node)
        assert image is not None
        assert caption is not None
        assert image.url == "https://cdn.example/pic.png"
        assert caption.caption == "A picture"


async def test_foreign_typename_reads_as_none(httpserver: HTTPServer):
    attachment: dict[str, object] = {
        "__typename": "LinkAttachment",
        "href": "https://example.com/post",
    }
    async with gql_server(
        httpserver,
        "slots_execute",
        {"Query": {"post": _resolve_post(attachment)}},
    ):
        result = await execute_queries.get_attachment.execute(
            id="p-1",
            attachment=[
                execute_queries.image_url,
                execute_queries.link_href,
            ],
        )
        assert result.post is not None
        node = result.post.attachment
        assert execute_queries.image_url.read(node) is None
        link = execute_queries.link_href.read(node)
        assert link is not None
        assert link.href == "https://example.com/post"


async def test_null_slot_node_reads_as_none(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_execute",
        {"Query": {"post": _resolve_post(None)}},
    ):
        result = await execute_queries.get_attachment.execute(
            id="p-1", attachment=execute_queries.image_url
        )
        assert result.post is not None
        assert execute_queries.image_url.read(result.post.attachment) is None


LINK_PAYLOAD: dict[str, object] = {
    "__typename": "LinkAttachment",
    "href": "https://example.com/post",
}


def _resolve_attachment_by_id(
    _root: None, _info: GraphQLResolveInfo, *, id: str
) -> dict[str, object]:
    return {"id": id, "attachment": {"img": IMAGE_PAYLOAD, "link": LINK_PAYLOAD}[id]}


async def test_union_fragment_reads_the_matching_variant(httpserver: HTTPServer):
    # A fragment whose type condition is the union itself, the counterpart of
    # the interface fragment `owner_identity` in slots_multi: its model is a
    # discriminated union of the variants it names, and `read` gives back the
    # one the node's __typename picked. Both payloads go through the same
    # handle, so a model that resolved to a fixed variant fails on one of them.
    async with gql_server(
        httpserver,
        "slots_execute",
        {"Query": {"post": _resolve_attachment_by_id}},
    ):
        image = await execute_queries.get_attachment.execute(
            id="img", attachment=execute_queries.attachment_identity
        )
        link = await execute_queries.get_attachment.execute(
            id="link", attachment=execute_queries.attachment_identity
        )
    assert image.post is not None
    assert link.post is not None
    assert execute_queries.attachment_identity.read(
        image.post.attachment
    ) == AttachmentIdentityDataImageAttachment(
        typename__="ImageAttachment", caption="A picture"
    )
    assert execute_queries.attachment_identity.read(
        link.post.attachment
    ) == AttachmentIdentityDataLinkAttachment(
        typename__="LinkAttachment", href="https://example.com/post"
    )


def test_handles_inherit_only_the_bases_they_are_spread_compatible_with():
    # Every slot type gets a base whether or not a fragment for it exists.
    # AlbumTitle is on a union member, OwnerIdentity on the interface itself,
    # and AlbumSummary is on a type no slot can hold — it never becomes a
    # handle at all. Checked on the real MRO, not the rendered text: the base
    # classes exist for isinstance/issubclass relationships and static
    # compatibility, and this is that relationship stated directly.
    # ImageAttachment is both an Attachment member and a Previewable
    # implementation, so its fragments carry two bases at once — the only
    # shape that exercises multiple generic bases.
    assert issubclass(multi_api.AlbumTitle, multi_api.AttachmentFragment)
    assert issubclass(multi_api.AlbumTitle, multi_api.PreviewableFragment)
    assert not issubclass(multi_api.AlbumTitle, multi_api.OwnerFragment)
    assert issubclass(multi_api.AlbumCover, multi_api.AttachmentFragment)
    assert issubclass(multi_api.AlbumCover, multi_api.PreviewableFragment)
    assert issubclass(multi_api.OwnerIdentity, multi_api.OwnerFragment)
    assert not issubclass(multi_api.OwnerIdentity, multi_api.AttachmentFragment)
    assert not issubclass(multi_api.OwnerIdentity, multi_api.PreviewableFragment)
    assert not hasattr(multi_api, "AlbumSummary")


FIRST_ATTACHMENT: dict[str, object] = {
    "__typename": "ImageAttachment",
    "url": "https://cdn.example/1.png",
    "album": {"id": "a-1", "title": "First", "cover": "cover-1"},
}

SECOND_ATTACHMENT: dict[str, object] = {
    "__typename": "ImageAttachment",
    "url": "https://cdn.example/2.png",
    "album": {"id": "a-2", "title": "Second", "cover": "cover-2"},
}

MULTI_ROWS: list[dict[str, object]] = [
    {
        "id": "p-1",
        # The same object behind two slots of different types: `attachment` sees it
        # as a union member, `preview` as an interface implementation.
        "attachment": FIRST_ATTACHMENT,
        "preview": FIRST_ATTACHMENT,
        "owner": {"__typename": "UserOwner", "id": "u-1", "email": "alice@example.com"},
    },
    {
        "id": "p-2",
        "attachment": SECOND_ATTACHMENT,
        "preview": SECOND_ATTACHMENT,
        "owner": {"__typename": "TeamOwner", "id": "t-1", "memberCount": 7},
    },
]


def _resolve_rows(_root: None, _info: GraphQLResolveInfo) -> list[dict[str, object]]:
    return MULTI_ROWS


def _not_none[T](value: T | None) -> T:
    assert value is not None
    return value


async def test_every_list_element_gets_its_own_slot_data(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        result = await multi_queries.list_posts.execute(
            attachment=multi_queries.album_title,
            preview=multi_queries.album_cover,
            owner=multi_queries.owner_identity,
        )
        titles = [
            _not_none(multi_queries.album_title.read(row.attachment)).album.title
            for row in result.posts
        ]
        assert titles == ["First", "Second"]


async def test_overlapping_nested_selections_stay_isolated(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        result = await multi_queries.list_posts.execute(
            attachment=[multi_queries.album_title, multi_queries.album_cover],
            preview=multi_queries.album_cover,
            owner=multi_queries.owner_identity,
        )
        node = result.posts[0].attachment
        # Both fragments select `album`, so the server merges their fields
        # into one node; each model exposes only its own selection of it.
        assert _not_none(multi_queries.album_title.read(node)).album.title == "First"
        assert _not_none(multi_queries.album_cover.read(node)).album.cover == "cover-1"


async def test_two_slots_are_read_independently(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        result = await multi_queries.list_posts.execute(
            attachment=multi_queries.album_title,
            preview=multi_queries.album_cover,
            owner=multi_queries.owner_identity,
        )
        owners = [
            _not_none(multi_queries.owner_identity.read(row.owner))
            for row in result.posts
        ]
        assert isinstance(owners[0], OwnerIdentityDataUserOwner)
        assert owners[0].email == "alice@example.com"
        assert isinstance(owners[1], OwnerIdentityDataTeamOwner)
        assert owners[1].member_count == 7
        # Reading another slot's node isn't caught statically, but it is loud
        # at runtime: this handle was never passed to the `attachment` slot,
        # so its data key is absent — which must not read as a legitimate
        # typename mismatch.
        with pytest.raises(ValueError, match="was not passed to slot 'attachment'"):
            multi_queries.owner_identity.read(result.posts[0].attachment)


async def test_slot_data_is_keyed_by_handle_identity(httpserver: HTTPServer):
    # Handle constructors are public, so the passed handle need not be the
    # module singleton — and a fresh instance of the same class is a different
    # handle whose read must fail as "was not passed", never alias the passed
    # instance's data. The node also keeps the passed instance alive: entries
    # are keyed by the handle object itself, so a released address can never
    # be recycled into a stored key.
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        passed = type(multi_queries.album_title)()
        result = await multi_queries.list_posts.execute(
            attachment=passed, preview=[], owner=[]
        )
        node = result.posts[0].attachment
        released = weakref.ref(passed)
        del passed
        kept = released()
        assert kept is not None
        assert _not_none(kept.read(node)).album.title == "First"
        fresh = type(multi_queries.album_title)()
        with pytest.raises(ValueError, match="was not passed to slot"):
            fresh.read(node)


async def test_one_handle_serves_two_slots_of_different_types(httpserver: HTTPServer):
    # AlbumTitle inherits both PreviewableFragment and AttachmentFragment,
    # so the same handle is accepted at both kwargs and reads back from both nodes.
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        result = await multi_queries.list_posts.execute(
            attachment=multi_queries.album_title,
            preview=multi_queries.album_title,
            owner=multi_queries.owner_identity,
        )
        row = result.posts[0]
        from_attachment = _not_none(multi_queries.album_title.read(row.attachment))
        from_preview = _not_none(multi_queries.album_title.read(row.preview))
        assert from_attachment.album.title == "First"
        assert from_preview.album.title == "First"


async def test_slot_without_fragments_sends_only_its_static_selection(
    httpserver: HTTPServer,
):
    # The one "no fragments" shape the feature can send: an explicit empty set
    # leaves the marker replaced by nothing and contributes no definitions.
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        result = await multi_queries.list_posts.execute(
            attachment=[], preview=[], owner=[]
        )
        # An explicit empty set means no handle was offered at all, so a read
        # is a wiring bug and fails loudly rather than blending into None.
        with pytest.raises(ValueError, match="was not passed to slot"):
            multi_queries.album_title.read(result.posts[0].attachment)
    request, _response = httpserver.log[-1]
    payload = pydantic.TypeAdapter(dict[str, str]).validate_json(
        request.get_data(as_text=True)
    )
    sent = payload["query"]
    assert "__slot__" not in sent
    assert "fragment" not in sent
    # The marker sits on its own line, so substituting nothing for it leaves the
    # line blank rather than removing it. GraphQL ignores the whitespace and the
    # node keeps its static selection; pinned here so a tidier substitution is a
    # deliberate change rather than an accident.
    assert "attachment {\n      __typename\n      \n    }" in sent


async def test_marker_like_text_in_the_operation_survives_splicing(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    # "__slot__item" written by the developer as an argument value must reach
    # the server untouched: splicing may only ever replace the synthesized
    # marker field, never matching text elsewhere in the document.
    received: list[str | None] = []

    def resolve_search(
        _obj: object, _info: GraphQLResolveInfo, term: str | None = None
    ) -> dict[str, str]:
        received.append(term)
        return {"id": "s-1"}

    async with test_project.server(
        httpserver,
        schema="""
        type Query {
            search(term: String): Item
            item: Item
        }

        type Item {
            id: ID!
        }
        """,
        queries='''
        from sample_app.gql.api import api_gql

        find = api_gql(
            """
            query Find {
                search(term: "__slot__item") { id }
                item @slot { __typename }
            }
            """
        )
        ''',
        resolvers={"Query": {"search": resolve_search, "item": lambda *_: {}}},
    ) as (_api_module, queries_module):
        await queries_module.find.execute(item=[])  # pyright: ignore[reportAny]
    assert received == ["__slot__item"]


async def test_assembled_source_carries_spreads_and_definitions(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        # Passed in the reverse of the sorted order (AlbumCover < AlbumTitle):
        # spreads and definitions are emitted sorted by fragment name, not in the
        # order the kwarg listed them. AlbumCover also reaches two slots at
        # once, so the definition counts below cover cross-slot dedup.
        _ = await multi_queries.list_posts.execute(
            attachment=[multi_queries.album_title, multi_queries.album_cover],
            preview=multi_queries.album_cover,
            owner=multi_queries.owner_identity,
        )
    request, _response = httpserver.log[-1]
    payload = pydantic.TypeAdapter(dict[str, str]).validate_json(
        request.get_data(as_text=True)
    )
    sent = payload["query"]
    attachment_selection = (
        "attachment {\n      __typename\n      ...AlbumCover ...AlbumTitle\n    }"
    )
    assert attachment_selection in sent
    assert "owner {\n      __typename\n      ...OwnerIdentity\n    }" in sent
    assert sent.count("fragment AlbumCover on ImageAttachment") == 1
    assert sent.count("fragment AlbumTitle on ImageAttachment") == 1
    assert sent.count("fragment OwnerIdentity on Owner") == 1


async def test_broken_slot_data_fails_execute(httpserver: HTTPServer):
    # A real server never returns a node without a requested non-null field, so
    # the broken payload is served as raw JSON — the same way
    # test_malformed_response_body does it in tests/test_runtime.py.
    def missing_field_handler(_request: Request) -> Response:
        body = {
            "data": {
                "post": {
                    "id": "p-1",
                    "attachment": {"__typename": "ImageAttachment"},
                }
            }
        }
        return Response(json.dumps(body), status=200, mimetype="application/json")

    httpserver.expect_request("/graphql/", method="POST").respond_with_handler(
        missing_field_handler
    )
    async with use_package_client("slots_execute", httpserver.url_for("/graphql/")):
        with pytest.raises(pydantic.ValidationError) as exc_info:
            _ = await execute_queries.get_attachment.execute(
                id="p-1", attachment=execute_queries.image_url
            )
    assert exc_info.value.errors()[0]["loc"] == (
        "post",
        "attachment",
        "ImageAttachment",
        "url",
    )


def _make_subscription_app(messages: list[dict[str, object]]) -> ASGIApp:
    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        connection = await accept_graphql_ws(scope, receive, send)
        subscription = await connection.ack()
        for msg in messages:
            await subscription.send_message(msg)
        await connection.drain()

    return app


FIRST_SUBSCRIPTION_ATTACHMENT: dict[str, object] = {
    "__typename": "ImageAttachment",
    "url": "https://cdn.example/1.png",
}

SECOND_SUBSCRIPTION_ATTACHMENT: dict[str, object] = {
    "__typename": "ImageAttachment",
    "url": "https://cdn.example/2.png",
}


async def test_subscription_validates_slot_on_every_message():
    # Eager validation runs per streamed message, so a test reading only the
    # first message would pass even if the slot context were dropped for every
    # message after it. Reading two consecutive messages with different
    # `url` values, and asserting both, is what pins that the context
    # is applied on each `next` rather than consumed once.
    messages: list[dict[str, object]] = [
        {
            "type": "next",
            "payload": {
                "data": {
                    "attachmentChanged": {
                        "id": "p-1",
                        "attachment": FIRST_SUBSCRIPTION_ATTACHMENT,
                    }
                }
            },
        },
        {
            "type": "next",
            "payload": {
                "data": {
                    "attachmentChanged": {
                        "id": "p-1",
                        "attachment": SECOND_SUBSCRIPTION_ATTACHMENT,
                    }
                }
            },
        },
        {"type": "complete"},
    ]
    app = _make_subscription_app(messages)

    async with use_package_client(
        "slots_subscription", "http://testserver/graphql", target_app=app
    ):
        events: list[str] = []
        async with subscription_queries.watch_attachment.execute(
            id="p-1", attachment=subscription_queries.image_url
        ) as stream:
            async for event in stream:
                image = subscription_queries.image_url.read(
                    event.attachment_changed.attachment
                )
                assert image is not None
                events.append(image.url)
        assert events == ["https://cdn.example/1.png", "https://cdn.example/2.png"]


POLY_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    owner: Owner
}

interface Owner {
    id: ID!
    profile: Profile
}

type UserOwner implements Owner {
    id: ID!
    profile: Profile
}

type TeamOwner implements Owner {
    id: ID!
    profile: Profile
}

type Profile {
    slug: String!
    name: String!
}
"""

POLY_QUERIES = '''
from sample_app.gql.api import api_gql

per_variant = api_gql(
    """
    fragment PerVariant on Owner {
        __typename
        ... on UserOwner { profile { name } }
        ... on TeamOwner { profile { slug } }
    }
    """
)

owner_slug = api_gql(
    """
    fragment OwnerSlug on Owner {
        profile { slug }
    }
    """
)

nested_owner = api_gql(
    """
    fragment NestedOwner on Post {
        owner {
            __typename
            ... on UserOwner { profile { name } }
            ... on TeamOwner { profile { slug } }
        }
    }
    """
)

get_post = api_gql(
    """
    query GetPost($id: ID!) {
        post(id: $id) {
            id
            owner @slot { __typename profile { slug } }
        }
    }
    """
)

get_box = api_gql(
    """
    query GetBox($id: ID!) {
        post(id: $id) @slot { __typename owner { __typename profile { slug } } }
    }
    """
)
'''


def _resolve_poly_post(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "id": "p1",
        "owner": {
            "__typename": "UserOwner",
            "id": "u1",
            "profile": {"slug": "slug-1", "name": "Alice"},
        },
    }


async def test_polymorphic_handle_reads_per_variant(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    # The fragment selects different fields per variant; the discriminated
    # union picks the current variant's own model, so each branch exposes
    # only its own fields, not the union of all variants.
    async with test_project.server(
        httpserver,
        schema=POLY_SCHEMA,
        queries=POLY_QUERIES,
        resolvers={"Query": {"post": _resolve_poly_post}},
    ) as (_api_module, queries_module):
        result = await queries_module.get_post.execute(  # pyright: ignore[reportAny]
            id="p1",
            owner=queries_module.per_variant,  # pyright: ignore[reportAny]
        )
        data = queries_module.per_variant.read(result.post.owner)  # pyright: ignore[reportAny]
        assert data is not None
        assert data.profile.name == "Alice"  # pyright: ignore[reportAny]


async def test_nested_polymorphic_selection_in_handle_reads_per_variant(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    # Same shape one level down: the fragment root is a plain object, the
    # polymorphism sits inside its selection, so the model must branch by
    # __typename at that position.
    async with test_project.server(
        httpserver,
        schema=POLY_SCHEMA,
        queries=POLY_QUERIES,
        resolvers={"Query": {"post": _resolve_poly_post}},
    ) as (_api_module, queries_module):
        result = await queries_module.get_box.execute(  # pyright: ignore[reportAny]
            id="p1",
            post=queries_module.nested_owner,  # pyright: ignore[reportAny]
        )
        data = queries_module.nested_owner.read(result.post)  # pyright: ignore[reportAny]
        assert data is not None
        assert data.owner.profile.name == "Alice"  # pyright: ignore[reportAny]


async def test_reading_with_a_handle_not_passed_to_the_slot_raises(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    # A handle that was never offered to the slot must fail loudly on read:
    # a silent None is indistinguishable from a legitimate typename mismatch
    # and hides wiring bugs forever.
    async with test_project.server(
        httpserver,
        schema=POLY_SCHEMA,
        queries=POLY_QUERIES,
        resolvers={"Query": {"post": _resolve_poly_post}},
    ) as (_api_module, queries_module):
        result = await queries_module.get_post.execute(  # pyright: ignore[reportAny]
            id="p1",
            owner=queries_module.per_variant,  # pyright: ignore[reportAny]
        )
        with pytest.raises(ValueError, match=r"'OwnerSlug' was not passed to slot"):
            queries_module.owner_slug.read(result.post.owner)  # pyright: ignore[reportAny]


async def test_schema_drift_typename_fails_loudly_on_interface_slot(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    # The server evolved: a new Owner implementation unknown to the generated
    # snapshot. A union slot already fails loudly on this; the interface slot
    # must too, instead of silently dropping data the server actually sent.
    httpserver.expect_request("/drift/", method="POST").respond_with_json({
        "data": {
            "post": {
                "id": "p1",
                "owner": {"__typename": "BotOwner", "id": "b1", "profile": None},
            }
        }
    })
    test_project.prepare(
        schema=POLY_SCHEMA,
        queries=POLY_QUERIES,
        base_url=httpserver.url_for("/drift/"),
    )
    api_module, queries_module = test_project.generate_and_import()
    try:
        with pytest.raises(pydantic.ValidationError, match="BotOwner"):
            await queries_module.get_post.execute(  # pyright: ignore[reportAny]
                id="p1",
                owner=queries_module.per_variant,  # pyright: ignore[reportAny]
            )
    finally:
        await api_module.API_CLIENT.close()  # pyright: ignore[reportAny]


def test_incompatible_fragment_error_names_the_slot_and_fragments(
    test_project: ProjectBuilder,
):
    # The headline of the combination probe is public wording; pinned so a
    # change of the validation strategy that loses it is deliberate.
    test_project.prepare(
        schema="""
        type Query {
            item: Item
        }

        type Item {
            id: ID!
            name: String
        }
        """,
        queries='''
        from sample_app.gql.api import api_gql

        clashing = api_gql("fragment Clashing on Item { value: name }")

        get_item = api_gql(
            """
            query GetItem {
                item @slot { __typename value: id }
            }
            """
        )
        ''',
    )
    with pytest.raises(GraphQLGenerationError, match=r"is incompatible with Clashing"):
        test_project.generate()


def test_slot_in_a_mutation_generates_the_same_kwarg_contract():
    # README promises @slot works in mutations; this pins the fixture through
    # the real signature of the generated `execute`, not the rendered text.
    parameter = inspect.signature(basic_api.Attach.execute).parameters["attachment"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    # inspect's stubs type both as Any; the generated module stringifies
    # annotations via `from __future__ import annotations`.
    default: object = parameter.default  # pyright: ignore[reportAny]
    annotation: object = parameter.annotation  # pyright: ignore[reportAny]
    assert default is inspect.Parameter.empty
    assert annotation == (
        "AttachmentFragment[pydantic.BaseModel]"
        " | Sequence[AttachmentFragment[pydantic.BaseModel]]"
    )


def test_slot_kwarg_is_snake_case_of_the_response_key(test_project: ProjectBuilder):
    # README documents the kwarg as snake_case of the slot field's name or
    # alias; the wire mapping keeps the original response key.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetPost($id: ID!) {
                post(id: $id) {
                    mainAttachment: attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    test_project.generate()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "main_attachment: AttachmentFragment[pydantic.BaseModel]" in generated
    assert '"mainAttachment": slots.as_handles(main_attachment)' in generated


def test_same_fragment_serves_both_roles_across_operations(
    test_project: ProjectBuilder,
):
    # README: the same fragment works spread by name in one operation and
    # passed into another operation's slot.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        image_url = api_gql(
            '''
            fragment ImageUrl on ImageAttachment { url }
            '''
        )

        by_name = api_gql(
            '''
            query ByName($id: ID!) {
                post(id: $id) {
                    attachment { __typename ... on ImageAttachment { url } }
                }
            }
            '''
        )

        by_slot = api_gql(
            '''
            query BySlot($id: ID!) {
                post(id: $id) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    test_project.generate()
    test_project.import_api()


async def test_subscription_surfaces_broken_data_in_a_later_message():
    # Validation of streamed messages is per-message: a first valid event must
    # come through and the second, broken one must raise from the stream — not
    # pass silently and not poison the first. The error class is the same
    # pydantic.ValidationError `execute` raises for a broken query response.
    messages: list[dict[str, object]] = [
        {
            "type": "next",
            "payload": {
                "data": {
                    "attachmentChanged": {
                        "id": "p-1",
                        "attachment": FIRST_SUBSCRIPTION_ATTACHMENT,
                    }
                }
            },
        },
        {
            "type": "next",
            "payload": {
                "data": {
                    "attachmentChanged": {
                        "id": "p-1",
                        "attachment": {"__typename": "ImageAttachment"},
                    }
                }
            },
        },
        {"type": "complete"},
    ]
    app = _make_subscription_app(messages)

    async with use_package_client(
        "slots_subscription", "http://testserver/graphql", target_app=app
    ):
        events: list[str] = []

        async def consume() -> None:
            async with subscription_queries.watch_attachment.execute(
                id="p-1", attachment=subscription_queries.image_url
            ) as stream:
                async for event in stream:
                    image = subscription_queries.image_url.read(
                        event.attachment_changed.attachment
                    )
                    assert image is not None
                    events.append(image.url)

        with pytest.raises(pydantic.ValidationError, match="url"):
            await consume()
        assert events == ["https://cdn.example/1.png"]


def test_slot_kwarg_is_mandatory_at_runtime():
    # Pinned as behavior, not as an annotation substring: there is no default,
    # so omitting the slot kwarg fails at the call site.
    with pytest.raises(TypeError, match="missing 1 required keyword-only argument"):
        basic_queries.get_attachment.execute(id="p-1")  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    "selection",
    [
        pytest.param("{ ...Tn }", id="through-a-fragment-spread"),
        pytest.param(
            "{ ... on ImageAttachment { __typename } }",
            id="only-inside-an-inline-fragment",
        ),
        pytest.param("{ tn: __typename }", id="behind-an-alias"),
    ],
)
def test_slot_typename_must_be_selected_directly(
    test_project: ProjectBuilder, selection: str
):
    # The predicate requires a direct, unaliased, undirected __typename field;
    # each of the four rejected forms must stay rejected — a relaxation to
    # "__typename appears somewhere in the subtree" keeps codegen green while
    # the runtime starts throwing on valid responses. The @include form is
    # pinned by test_slot_without_typename_is_rejected.
    test_project.prepare(
        schema=SCHEMA,
        queries=f"""
        from sample_app.gql.api import api_gql

        tn = api_gql(
            '''
            fragment Tn on Attachment {{ __typename }}
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($id: ID!) {{
                post(id: $id) {{
                    attachment @slot {selection}
                }}
            }}
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match="must select __typename unconditionally"
    ):
        test_project.generate()


def test_slot_directive_already_in_schema_is_accepted_when_identical(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema="directive @slot on FIELD\n" + SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql
        """
        + SLOT_OPERATION,
    )
    assert test_project.generate() is True


def test_slot_directive_already_in_schema_is_rejected_when_different(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema="directive @slot(reason: String) on FIELD\n" + SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql
        """
        + SLOT_OPERATION,
    )
    with pytest.raises(GraphQLGenerationError, match=r"declares @slot differently"):
        test_project.generate()


def test_slot_twin_with_a_non_slot_selection_names_both_origins(
    test_project: ProjectBuilder,
):
    # The `_slot_origin` wording for the non-slot side of a raw-name twin.
    test_project.prepare(
        schema="""
        type Query {
            aB: W1
            a: W2
        }

        type W1 {
            c: Item
        }

        type W2 {
            bCSlot: Item
        }

        type Item {
            id: ID!
            name: String
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query Q {
                aB { c @slot { __typename } }
                a { bCSlot { id } }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match=r"slot 'c' and by a non-slot selection"
    ):
        test_project.generate()


def _resolve_isolation_board(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "__typename": "Board",
        "id": "b-1",
        "owner": {"fullName": "Alice"},
        "cards": [{"title": "T"}],
        "activity": None,
    }


async def test_aliased_slot_executes_end_to_end(httpserver: HTTPServer):
    # `main: board @slot` — the kwarg is the alias, the wire key is the alias,
    # and the response validates through the node; previously pinned only by
    # the generated module's text.
    async with gql_server(
        httpserver,
        "slots_isolation",
        {"Query": {"board": _resolve_isolation_board}},
    ):
        result = await isolation_queries.ping_main.execute(id="b-1", main=[])
        assert result.main is not None
        assert result.main.typename__ == "Board"


async def test_merged_key_slot_executes_end_to_end(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_isolation",
        {"Query": {"board": _resolve_isolation_board}},
    ):
        result = await isolation_queries.merged_board.execute(id="b-1", merged=[])
        assert result.merged is not None
        assert result.merged.typename__ == "Board"


def _resolve_lists_board(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "__typename": "Board",
        "id": "b-1",
        "cards": [{"title": "First"}, {"title": "Second"}],
        "events": [
            {"__typename": "Comment", "body": "hi", "author": {"fullName": "Alice"}},
            {"__typename": "Move", "fromColumn": "todo", "author": {"id": "u-1"}},
        ],
    }


async def test_list_of_union_projects_each_element_through_its_variant(
    httpserver: HTTPServer,
):
    # `events: [Activity!]!` puts a discriminated union under a list: every
    # element picks its own variant branch, so a Comment element validates as
    # the Comment variant and a Move as Move — through one handle in one
    # response.
    async with gql_server(
        httpserver,
        "slots_lists",
        {"Query": {"board": _resolve_lists_board}},
    ):
        result = await lists_queries.get_events.execute(
            id="b-1", board=lists_queries.activity_texts
        )
    assert result.board is not None
    data = lists_queries.activity_texts.read(result.board)
    assert data is not None
    comment, move = data.events
    assert isinstance(comment, ActivityTextsDataEventsComment)
    assert comment.body == "hi"
    assert isinstance(move, ActivityTextsDataEventsMove)
    assert move.from_column == "todo"


async def test_slot_on_a_list_field_gives_each_element_its_own_node(
    httpserver: HTTPServer,
):
    # `cards @slot` sits directly on a list field: every element is its own
    # slot node carrying its own data entry for the handle.
    async with gql_server(
        httpserver,
        "slots_lists",
        {"Query": {"board": _resolve_lists_board}},
    ):
        result = await lists_queries.get_cards.execute(
            id="b-1", cards=lists_queries.card_title
        )
    assert result.board is not None
    cards = [lists_queries.card_title.read(node) for node in result.board.cards]
    assert [card.title for card in cards if card is not None] == ["First", "Second"]
