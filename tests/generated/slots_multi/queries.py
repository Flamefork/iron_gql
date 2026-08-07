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
