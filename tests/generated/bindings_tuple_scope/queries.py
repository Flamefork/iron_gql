from tests.generated.bindings_tuple_scope.gql.api import api_gql

image_url = api_gql(
    """
    fragment ImageUrl on ImageAttachment {
        url
    }
    """
)

link_url = api_gql(
    """
    fragment LinkUrl on LinkAttachment {
        href
    }
    """
)

get_post_attachment = api_gql(
    """
    query GetPostAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
    """
)

get_page_attachment = api_gql(
    """
    query GetPageAttachment($id: ID!) {
        page(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
    """
)

# The package's only literal tuple bind, and only on GetPostAttachment: the
# combination it discovers must not become an overload GetPageAttachment
# also carries, since GetPageAttachment's own "attachment" slot cannot spread
# either fragment (it is a Media, not an Attachment).
post_attachment = get_post_attachment.bind(attachment=(image_url, link_url))
