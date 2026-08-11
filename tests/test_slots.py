import inspect
import json

import pydantic
import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer
from werkzeug import Request
from werkzeug import Response

from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.conftest import generated_source
from tests.conftest import gql_server
from tests.conftest import make_subscription_app
from tests.conftest import read_type_erased
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

    image_url = api_gql(
        """
        fragment ImageUrl on ImageAttachment {
            url
        }
        """
    )

    attach_image = attach.bind(attachment=image_url)
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

    board_id = api_gql(
        """
        fragment BoardId on Board {
            id
        }
        """
    )

    ping_main_bare = ping_main.bind()
    # A template whose only binding is the all-unfilled one renders a single
    # `@overload` for `bind()`, which basedpyright always flags
    # (reportInconsistentOverload -- it requires 2+ overload variants); this
    # second, otherwise-unused binding keeps the overload count at 2 so the
    # bare one below stays checkable.
    ping_main_typed = ping_main.bind(main=board_id)
    merged_board_bare = merged_board.bind()
    merged_board_typed = merged_board.bind(merged=board_id)
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

    get_events_with_texts = get_events.bind(board=activity_texts)
    get_cards_with_titles = get_cards.bind(cards=card_title)
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

    link_href = api_gql(
        """
        fragment LinkHref on LinkAttachment {
            href
        }
        """
    )

    # A fragment used only inside the multi-fragment `attachment` list below:
    # reusing `image_url` there (already bound alone in `get_image`) would
    # render a `Sequence[ImageUrl | LinkHref]` overload that basedpyright
    # flags as overlapping `get_image`'s own `Sequence[ImageUrl]` overload.
    image_caption = api_gql(
        """
        fragment ImageCaption on ImageAttachment {
            caption
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

    get_image = get_attachment.bind(attachment=image_url)
    get_image_or_link = get_attachment.bind(attachment=[image_caption, link_href])
    get_identity = get_attachment.bind(attachment=attachment_identity)
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

    link_href = api_gql(
        """
        fragment LinkHref on LinkAttachment {
            href
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

    # `album_summary` stays deliberately unbound here: it is a fragment on
    # Album, a type outside every slot's possible types in this package, so
    # it can never be bound (test_fragment_handles.py's basedpyright test
    # relies on it staying the untyped passthrough).
    list_posts_typed = list_posts.bind(
        attachment=album_title, preview=album_cover, owner=owner_identity
    )
    # A slot's list may mix fragments whose runtime-type coverage overlaps --
    # each reads its own slice of the payload independently. LinkHref and
    # AlbumCover happen to be type-disjoint here regardless, but that is not
    # why they are paired: AlbumCover stands in for AlbumTitle only because
    # AlbumTitle already appears alone in
    # `list_posts_typed`/`list_posts_shared_handle`'s `attachment`, and
    # reusing it in this list would render a `Sequence[AlbumTitle |
    # LinkHref]` overload that basedpyright flags as overlapping the existing
    # bare-AlbumTitle overloads' own Sequence form. A multi-fragment slot
    # loses the single-handle overload for the whole combo, so the only
    # overload left types every filled slot as a Sequence -- preview/owner
    # must be spelled as one-element lists here to match it (bind_key
    # normalizes either spelling to the same dispatch key, so this is a
    # static-typing concern only).
    list_posts_dual = list_posts.bind(
        attachment=[link_href, album_cover],
        preview=[album_cover],
        owner=[owner_identity],
    )
    list_posts_shared_handle = list_posts.bind(
        attachment=album_title, preview=album_title, owner=owner_identity
    )
    list_posts_bare = list_posts.bind()
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

    watch_image = watch_attachment.bind(attachment=image_url)
    ''',
)


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
from tests.generated.slots_multi.gql.api import OwnerIdentityDataTeamOwner
from tests.generated.slots_multi.gql.api import OwnerIdentityDataUserOwner
from tests.generated.slots_subscription import queries as subscription_queries


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


def test_conditional_slot_is_rejected(test_project: ProjectBuilder):
    # A slot field is always requested: its spreads are inserted wherever the
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
    # field from the collected models, but the slot itself is still derived
    # from the AST — the template would promise fragment data on that slot
    # that can never arrive, and the rendered module would reference the
    # never-imported `slots` runtime.
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

        bound = get_attachment.bind(attachment=caption_as_value)
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="Fields 'value' conflict"):
        test_project.generate()


def test_compatible_fragments_that_merge_cleanly_are_accepted(
    test_project: ProjectBuilder,
):
    # `Wrapper` is the one fragment directly bound; `UrlAsIs` reaches the slot
    # transitively through `Wrapper`'s own spread. The two must still merge
    # cleanly at the full-pipeline level, or it would reject the feature's
    # main use case — the conflicting counterpart of this shape is pinned
    # directly on expand_binding by
    # test_binding_expansion.py::test_merge_conflict_across_fragments_reported.
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

        wrapper = api_gql(
            '''
            fragment Wrapper on ImageAttachment {
                value: caption
                ...UrlAsIs
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

        bound = get_attachment.bind(attachment=wrapper)
        """,
    )
    assert test_project.generate() is True


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
        + SLOT_OPERATION
        + """
        bound = get_attachment.bind(attachment=client)
        """,
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
    with pytest.raises(
        GraphQLGenerationError,
        match=r"Parameter 'self' of execute\(\) of operation .* is claimed by",
    ):
        test_project.generate()


# An input type spelled exactly like the type parameter of a bound base, and
# used as a template's variable so the name reaches the one scope that base
# writes.
BOUND_PARAM_SCHEMA = """
type Query {
    post(filter: TResult!): Post
}

input TResult {
    id: ID!
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}
"""


def test_input_named_after_the_bound_base_parameter_is_rejected(
    test_project: ProjectBuilder,
):
    # The parameter lives inside the base, where it shadows the module-level
    # input model: `execute(*, filter: TResult)` reads as the parameter there
    # -- pinned to the binding's result by `Bound[Result]` -- and as the model
    # in the binding's override, which is an incompatible override rather than
    # anything the generator would notice on its own.
    test_project.prepare(
        schema=BOUND_PARAM_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        url = api_gql(
            '''
            fragment Url on ImageAttachment { url }
            '''
        )

        get_attachment = api_gql(
            '''
            query GetAttachment($filter: TResult!) {
                post(filter: $filter) {
                    attachment @slot { __typename }
                }
            }
            '''
        )

        bound = get_attachment.bind(attachment=url)
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="'TResult' is claimed by"):
        test_project.generate()


SLOT_PARAM_INPUT_SCHEMA = """
type Query {
    post(execute: TSlotAttachment!): Post
}

input TSlotAttachment {
    id: ID!
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}
"""


def test_input_named_like_a_slot_parameter_is_valid_outside_generic_models(
    test_project: ProjectBuilder,
):
    # The input model is referenced by execute() on the bound base, while the
    # same-spelled slot parameter exists only inside generic result artifacts.
    # They occupy different scopes, so the generated package must import.
    test_project.prepare(
        schema=SLOT_PARAM_INPUT_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment($execute: TSlotAttachment!) {
                post(execute: $execute) {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    test_project.generate_and_import()


SLOT_PARAM_SHADOW_SCHEMA = """
type Query {
    post: Post
}

type Post {
    kind: TSlotAttachment!
    attachment: Attachment
}

enum TSlotAttachment {
    PHOTO
    VIDEO
}

type Attachment {
    id: ID!
}
"""


def test_slot_parameter_shadowing_a_type_in_a_generic_model_is_rejected(
    test_project: ProjectBuilder,
):
    # Post declares TSlotAttachment for the slot path and also refers to the
    # module-level enum in `kind`. The local parameter would change that field
    # annotation from the enum to the binding's fragment type.
    test_project.prepare(
        schema=SLOT_PARAM_SHADOW_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        get_attachment = api_gql(
            '''
            query GetAttachment {
                post {
                    kind
                    attachment @slot { __typename }
                }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=(
            r"Parameter 'TSlotAttachment' of generic artifact 'Post'"
            r".*referenced type 'TSlotAttachment'"
        ),
    ):
        test_project.generate()


# A result subtree that includes enum fields under a slot.
ENUM_IN_SLOT_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

enum Kind {
    PHOTO
    VIDEO
}

type ImageAttachment {
    url: String!
    kind: Kind!
    thumb: Thumb!
}

type Thumb {
    label: String!
    kind: Kind!
}

type LinkAttachment {
    href: String!
}
"""


def test_a_bound_fragment_selecting_an_enum_generates(test_project: ProjectBuilder):
    # `reachable_model_names` walks the slot's subtree and has to step over
    # two kinds of dependency that are not artifacts to recurse into: an enum,
    # which is a leaf, and a model already reached by another path. Narrowing
    # the nested-slot check to templates left both untested -- and without the
    # enum half the walk raises KeyError for every package that selects an
    # enum field inside a bound fragment.
    test_project.prepare(
        schema=ENUM_IN_SLOT_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        image_parts = api_gql(
            '''
            fragment ImageParts on ImageAttachment {
                url
                kind
                thumb { label kind }
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

        bound = with_slot.bind(attachment=image_parts)
        """,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "type Kind = Literal['PHOTO', 'VIDEO']" in generated


def test_two_templates_may_name_a_slot_alike(test_project: ProjectBuilder):
    # A slot's name is scoped to its own template: it is a `bind()` keyword of
    # that template's class and nothing else, and every model it reaches is
    # named after the template or the binding that produced it. So two
    # templates with an `attachment` slot share no namespace, and the
    # module-name rules must not claim otherwise.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        url = api_gql(
            '''
            fragment Url on ImageAttachment { url }
            '''
        )

        first = api_gql(
            '''
            query First($id: ID!) {
                post(id: $id) { attachment @slot { __typename } }
            }
            '''
        )

        second = api_gql(
            '''
            query Second($id: ID!) {
                post(id: $id) { attachment @slot { __typename } }
            }
            '''
        )

        one = first.bind(attachment=url)
        two = second.bind(attachment=url)
        """,
    )
    assert test_project.generate() is True


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

        bound = with_slot.bind(attachment=url)
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
        + SLOT_OPERATION
        + """
        bound = get_attachment.bind(attachment=url)
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="'N' is claimed by"):
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

        bound = get_attachment.bind(attachment=image_attachment)
        """,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class ImageAttachment(slots.GQLFragment[ImageAttachmentData]):" in generated
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
    assert "class GetBoardResult[TSlotBoard = Never](GQLModel):" in generated
    assert GetBoardResultBoardSlot.model_config.get("extra") == "ignore"
    assert GetBoardResult.model_config.get("extra") == "forbid"


def test_slots_with_equal_static_selections_are_not_deduplicated():
    # PingBoard, PingMain and MergedBoard all select the same `{ __typename }`
    # on the same type, so the name-dedup pass would collapse them into a
    # single class whose one `slot_name__` would then have to speak for three
    # slots. GetBoard's richer selection makes the fourth.
    #
    # Which fragments a binding offers no longer separates them either: the
    # node is one class per slot, generic in that slot's phantom, so two slots
    # that would collapse under the name-dedup pass are told apart by their
    # own `slot_name__` and by nothing else.
    generated = generated_source("slots_isolation")
    for name, param in (
        ("GetBoardResultBoardSlot", "TSlotBoard"),
        ("PingBoardResultBoardSlot", "TSlotBoard"),
        ("PingMainResultMainSlot", "TSlotMain"),
        ("MergedBoardResultMergedSlot", "TSlotMerged"),
    ):
        header = f"class {name}[{param} = Never]"
        assert f"{header}(GQLSlotModel[{param}]):" in generated


def test_slot_is_detected_on_any_node_of_a_merged_response_key():
    # `merged` comes from two field nodes and only the second carries `@slot`
    # (a shared fragment selecting the field plus `@slot` in the operation is
    # the realistic shape of this). Slot collection and exec-source stripping
    # both fire on any node with the directive, so the model must too —
    # otherwise the template still has the slot in its bind() surface but a
    # plain model that cannot hold fragment data.
    generated = generated_source("slots_isolation")
    assert 'slot_name__: ClassVar[str] = "merged"' in generated


def test_slot_subtree_ignores_foreign_fields_at_every_depth():
    # The server returns the union of every consumer's selection inside a slot,
    # so a node in the slot subtree sees fields it never asked for. The open
    # config ignores those, while the models expose exactly their own
    # selection. An empty handle tuple stands in for the fragments that would
    # have selected `email`/`position`/`authorId`. `GetBoard` is a template
    # nothing binds, so its result models are the shared ones and its slot
    # node's phantom is `Never` -- nothing is readable there, which the empty
    # handle tuple below is the runtime half of.
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
        result = await execute_queries.get_image.execute(id="p-1")
        assert result.post is not None
        image = execute_queries.image_url.read(result.post.attachment)
        assert image is not None
        assert image.url == "https://cdn.example/pic.png"


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
        result = await execute_queries.get_image_or_link.execute(id="p-1")
        assert result.post is not None
        node = result.post.attachment
        assert execute_queries.image_caption.read(node) is None
        link = execute_queries.link_href.read(node)
        assert link is not None
        assert link.href == "https://example.com/post"


async def test_null_slot_node_reads_as_none(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_execute",
        {"Query": {"post": _resolve_post(None)}},
    ):
        result = await execute_queries.get_image.execute(id="p-1")
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
        image = await execute_queries.get_identity.execute(id="img")
        link = await execute_queries.get_identity.execute(id="link")
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
        result = await multi_queries.list_posts_typed.execute()
        titles = [
            _not_none(multi_queries.album_title.read(row.attachment)).album.title
            for row in result.posts
        ]
        assert titles == ["First", "Second"]


async def test_two_slots_are_read_independently(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        result = await multi_queries.list_posts_typed.execute()
        owners = [
            _not_none(multi_queries.owner_identity.read(row.owner))
            for row in result.posts
        ]
        assert isinstance(owners[0], OwnerIdentityDataUserOwner)
        assert owners[0].email == "alice@example.com"
        assert isinstance(owners[1], OwnerIdentityDataTeamOwner)
        assert owners[1].member_count == 7
        # This handle was never passed to the `attachment` slot, so its data
        # key is absent — which must not read as a legitimate typename
        # mismatch. The phantom rejects the read statically; through a
        # type-erased path it stays loud at runtime.
        with pytest.raises(
            ValueError,
            match="is not part of the binding that produced slot 'attachment'",
        ):
            read_type_erased(multi_queries.owner_identity, result.posts[0].attachment)


async def test_one_handle_serves_two_slots_of_different_types(httpserver: HTTPServer):
    # AlbumTitle is spread-compatible with both `attachment` and `preview`, so
    # the same fragment can be bound into both slots at once and reads back
    # independently from both nodes.
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        result = await multi_queries.list_posts_shared_handle.execute()
        row = result.posts[0]
        from_attachment = _not_none(multi_queries.album_title.read(row.attachment))
        from_preview = _not_none(multi_queries.album_title.read(row.preview))
        assert from_attachment.album.title == "First"
        assert from_preview.album.title == "First"


async def test_slot_without_fragments_sends_only_its_static_selection(
    httpserver: HTTPServer,
):
    # The one "no fragments" shape the feature can send: the exec source is
    # static text fixed at codegen time, and an all-empty bind's text carries
    # no spreads and no fragment definitions at all.
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        result = await multi_queries.list_posts_bare.execute()
        # No handle was ever bound into this slot, so its phantom is `Never`
        # and a read is a wiring bug: statically rejected, and still loud at
        # runtime through a type-erased path rather than blending into None.
        with pytest.raises(
            ValueError, match="is not part of the binding that produced slot"
        ):
            read_type_erased(multi_queries.album_title, result.posts[0].attachment)
    request, _response = httpserver.log[-1]
    payload = pydantic.TypeAdapter(dict[str, str]).validate_json(
        request.get_data(as_text=True)
    )
    sent = payload["query"]
    assert "@slot" not in sent
    assert "..." not in sent
    assert "fragment" not in sent
    assert "attachment {" in sent


async def test_assembled_source_carries_spreads_and_definitions(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_multi",
        {"Query": {"posts": _resolve_rows}},
    ):
        # `list_posts_dual` binds `attachment` in reverse of the sorted order
        # (AlbumCover < LinkHref): spreads and definitions are emitted sorted
        # by fragment name, not in the order the bind call listed them.
        # `preview` reaches AlbumCover through a separate slot of its own, so
        # the definition counts below also cover cross-slot dedup.
        _ = await multi_queries.list_posts_dual.execute()
    request, _response = httpserver.log[-1]
    payload = pydantic.TypeAdapter(dict[str, str]).validate_json(
        request.get_data(as_text=True)
    )
    sent = payload["query"]
    attachment_selection = (
        "attachment {\n      __typename\n      ...AlbumCover\n      ...LinkHref\n    }"
    )
    assert attachment_selection in sent
    assert "owner {\n      __typename\n      ...OwnerIdentity\n    }" in sent
    assert sent.count("fragment AlbumCover on ImageAttachment") == 1
    assert sent.count("fragment LinkHref on LinkAttachment") == 1
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
            _ = await execute_queries.get_image.execute(id="p-1")
    assert exc_info.value.errors()[0]["loc"] == (
        "post",
        "attachment",
        "ImageAttachment",
        "url",
    )


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
    app = make_subscription_app(messages)

    async with use_package_client(
        "slots_subscription", "http://testserver/graphql", target_app=app
    ):
        events: list[str] = []
        async with subscription_queries.watch_image.execute(id="p-1") as stream:
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

get_post_variant = get_post.bind(owner=per_variant)
get_box_nested = get_box.bind(post=nested_owner)
# Bound only to make `owner_slug` a real handle at all -- unused elsewhere in
# this module, so its closure never reaches `get_post_variant`.
get_post_by_slug = get_post.bind(owner=owner_slug)
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
        result = await queries_module.get_post_variant.execute(id="p1")  # pyright: ignore[reportAny]
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
        result = await queries_module.get_box_nested.execute(id="p1")  # pyright: ignore[reportAny]
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
        result = await queries_module.get_post_variant.execute(id="p1")  # pyright: ignore[reportAny]
        with pytest.raises(
            ValueError,
            match=r"'OwnerSlug' is not part of the binding that produced slot",
        ):
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
            await queries_module.get_post_variant.execute(id="p1")  # pyright: ignore[reportAny]
    finally:
        await api_module.API_CLIENT.close()  # pyright: ignore[reportAny]


def test_slot_in_a_mutation_generates_the_same_kwarg_contract():
    # README promises @slot works in mutations; this pins the fixture through
    # the real signature of the generated binding's `execute`, not the
    # rendered text. The binding's execute takes only the template's own
    # variables — fragment selection happens at bind time, not at call time.
    parameters = inspect.signature(
        basic_api.AttachWithAttachmentImageUrl.execute
    ).parameters
    assert list(parameters) == ["self", "id"]
    assert parameters["id"].kind is inspect.Parameter.KEYWORD_ONLY
    default: object = parameters["id"].default  # pyright: ignore[reportAny]
    annotation: object = parameters["id"].annotation  # pyright: ignore[reportAny]
    assert default is inspect.Parameter.empty
    assert annotation == "builtins.str"


def test_slot_kwarg_is_snake_case_of_the_response_key(test_project: ProjectBuilder):
    # README documents the bind() kwarg as snake_case of the slot field's name
    # or alias; the wire mapping keeps the original response key. Both halves
    # have to agree on one name: the keyword discovery reads from the source
    # and the parameter the generator renders. A response key that is already
    # snake_case hides a disagreement between them, so this one is not -- and
    # the generated module is imported, which re-executes the very
    # `q.bind(main_attachment=...)` call the package was generated from.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        image_url = api_gql(
            '''
            fragment ImageUrl on ImageAttachment { url }
            '''
        )

        q = api_gql(
            '''
            query GetPost($id: ID!) {
                post(id: $id) {
                    mainAttachment: attachment @slot { __typename }
                }
            }
            '''
        )

        bound = q.bind(main_attachment=image_url)
        """,
    )
    _api_module, queries_module = test_project.generate_and_import()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    # A template with exactly one binding has no `@overload` at all --
    # `@overload` needs two or more signatures, and one binding has only one
    # shape to declare -- so `bind()` renders as that one typed signature
    # directly, with a real body (see `_bind_single_binding_impl`).
    assert (
        "def bind(self, *, main_attachment: ImageUrl | Sequence[ImageUrl]) "
        "-> GetPostWithMainAttachmentImageUrl:"
    ) in generated
    assert (
        '"mainAttachment": (slots.SlotHandle(IMAGE_URL, '
        "frozenset({'ImageAttachment'})),)"
    ) in generated
    # attributes of a dynamically imported module are Any
    assert (
        type(queries_module.bound).__name__  # pyright: ignore[reportAny]
        == "GetPostWithMainAttachmentImageUrl"
    )


def test_bind_keyword_spelled_as_the_raw_response_key_is_rejected(
    test_project: ProjectBuilder,
):
    # The counterpart of the rule above: a bind that names the slot by its
    # GraphQL response key must be rejected at generation, naming the snake
    # spelling. Accepting it produced a class whose own `bind()` signature
    # could not be called with the keyword that created it -- an import-time
    # TypeError in the user's own module.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        image_url = api_gql(
            '''
            fragment ImageUrl on ImageAttachment { url }
            '''
        )

        q = api_gql(
            '''
            query GetPost($id: ID!) {
                post(id: $id) {
                    mainAttachment: attachment @slot { __typename }
                }
            }
            '''
        )

        bound = q.bind(mainAttachment=image_url)
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=r"unknown slot 'mainAttachment'.*slots are: main_attachment",
    ):
        test_project.generate()


def test_same_fragment_serves_both_roles_across_operations(
    test_project: ProjectBuilder,
):
    # README: the same fragment works spread by name in one operation and
    # bound into another operation's slot.
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
                    attachment { __typename ... on ImageAttachment { ...ImageUrl } }
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

        bound = by_slot.bind(attachment=image_url)
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
    app = make_subscription_app(messages)

    async with use_package_client(
        "slots_subscription", "http://testserver/graphql", target_app=app
    ):
        events: list[str] = []

        async def consume() -> None:
            async with subscription_queries.watch_image.execute(id="p-1") as stream:
                async for event in stream:
                    image = subscription_queries.image_url.read(
                        event.attachment_changed.attachment
                    )
                    assert image is not None
                    events.append(image.url)

        with pytest.raises(pydantic.ValidationError, match="url"):
            await consume()
        assert events == ["https://cdn.example/1.png"]


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
        result = await isolation_queries.ping_main_bare.execute(id="b-1")
        assert result.main is not None
        assert result.main.typename__ == "Board"


async def test_merged_key_slot_executes_end_to_end(httpserver: HTTPServer):
    async with gql_server(
        httpserver,
        "slots_isolation",
        {"Query": {"board": _resolve_isolation_board}},
    ):
        result = await isolation_queries.merged_board_bare.execute(id="b-1")
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
        result = await lists_queries.get_events_with_texts.execute(id="b-1")
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
        result = await lists_queries.get_cards_with_titles.execute(id="b-1")
    assert result.board is not None
    cards = [lists_queries.card_title.read(node) for node in result.board.cards]
    assert [card.title for card in cards if card is not None] == ["First", "Second"]


# --- Namespaces the generator writes parameters into ------------------------

TWO_ATTACHMENT_SCHEMA = """
type Query {
    post(id: ID!): Post
    comment(id: ID!): Comment
}

type Post {
    id: ID!
    attachment: Attachment
}

type Comment {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}
"""


def test_template_execute_parameter_that_is_a_keyword_is_rejected(
    test_project: ProjectBuilder,
):
    # An operation with a slot is a template, not a `CollectedOperation`, and
    # its `execute` renders from `template.variables` -- a namespace of its
    # own. `$class` reaches it as `class`, which parses but never compiles.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        image_url = api_gql(
            '''
            fragment ImageUrl on ImageAttachment { url }
            '''
        )

        q = api_gql(
            '''
            query GetPost($class: ID!) {
                post(id: $class) {
                    id
                    attachment @slot { __typename }
                }
            }
            '''
        )

        bound = q.bind(attachment=image_url)
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=r"Parameter 'class' of execute\(\) of template 'GetPost'",
    ):
        test_project.generate()


def test_two_slots_mapping_to_one_python_name_are_rejected(
    test_project: ProjectBuilder,
):
    # `att` and `Att` are two response keys and two slot models, but one
    # `bind()` keyword and one `TAtt` type parameter -- the generated class
    # body would declare each of them twice.
    test_project.prepare(
        schema=TWO_ATTACHMENT_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetBoth($id: ID!) {
                post(id: $id) { att: attachment @slot { __typename } }
                comment(id: $id) { Att: attachment @slot { __typename } }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=r"Slots 'att', 'Att' of template 'GetBoth'.*map to the Python name 'att'",
    ):
        test_project.generate()


def test_two_slots_collapsing_to_one_type_parameter_are_rejected(
    test_project: ProjectBuilder,
):
    # `details` and `_details` are two `bind()` keywords -- the keyword gate
    # above has nothing to say about them -- but one type parameter, because
    # the phantom's name drops the underscores. Left alone, the result model
    # would declare one parameter while every binding of it passes two
    # arguments, and the generated package would fail to import.
    test_project.prepare(
        schema=TWO_ATTACHMENT_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetBoth($id: ID!) {
                post(id: $id) { details: attachment @slot { __typename } }
                comment(id: $id) { _details: attachment @slot { __typename } }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError,
        match=(
            r"Parameter 'TSlotDetails' of the type parameters of template "
            r"'GetBoth'.*is claimed by"
        ),
    ):
        test_project.generate()


def test_one_slot_name_under_two_parents_collects_both_positions_models(
    test_project: ProjectBuilder,
):
    # One response key, two positions: both carry the same spliced fragments,
    # so the slot's node types collect both positions' models rather than one
    # picked by collection order.
    test_project.prepare(
        schema=TWO_ATTACHMENT_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetBoth($id: ID!) {
                post(id: $id) { attachment @slot { __typename } }
                comment(id: $id) { attachment @slot { __typename } }
            }
            '''
        )
        """,
    )
    test_project.generate()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "type GetBothResultPostAttachmentSlot[TSlotAttachment] = " in generated
    assert "type GetBothResultCommentAttachmentSlot[TSlotAttachment] = " in generated


POLYMORPHIC_SLOT_PARENT_SCHEMA = """
type Query {
    item: Item
}

interface Item {
    id: ID!
    detail: Detail!
}

type Post implements Item {
    id: ID!
    detail: Detail!
    title: String!
}

type Note implements Item {
    id: ID!
    detail: Detail!
}

type Detail {
    body: String!
}
"""


def test_a_slot_under_a_polymorphic_parent_collects_each_variants_model(
    test_project: ProjectBuilder,
):
    # One source position for `detail @slot`, but the parent is an interface
    # with an explicit variant, so the collector builds one slot node model per
    # variant. That split is the collector's own, not two parents a developer
    # could alias apart -- generation must not reject it, and the slot's node
    # types collect every variant's model.
    test_project.prepare(
        schema=POLYMORPHIC_SLOT_PARENT_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query Feed {
                item {
                    __typename
                    detail @slot { __typename }
                    ... on Post { title }
                }
            }
            '''
        )
        """,
    )
    test_project.generate()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class FeedResultItemPostDetailSlot[" in generated
    assert "class FeedResultItemItemDetailSlot[" in generated
