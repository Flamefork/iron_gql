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

# `album_summary` is a fragment on Album, a type outside every slot's
# possible types in this package: it still gets a class and an `OnAlbum`
# base, and what makes it unbindable is that no slot's signature names
# that base (test_fragment_handles.py's basedpyright test relies on it).
list_posts_typed = list_posts.bind(
    attachment=album_title, preview=album_cover, owner=owner_identity
)
# A slot's list may mix fragments whose runtime-type coverage overlaps --
# each reads its own slice of the payload independently. Only the
# multi-fragment slot is spelled as a tuple: each slot picks its own form
# independently now, and a one-element list has no form of its own (a
# single fragment is accepted through its on-type base instead).
list_posts_dual = list_posts.bind(
    attachment=(link_href, album_cover),
    preview=album_cover,
    owner=owner_identity,
)
list_posts_shared_definition = list_posts.bind(
    attachment=album_title, preview=album_title, owner=owner_identity
)
list_posts_bare = list_posts.bind()
