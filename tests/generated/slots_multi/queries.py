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
