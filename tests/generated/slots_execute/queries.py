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

# A fragment of its own for the multi-fragment `attachment` bind below,
# rather than reusing `image_url` from `get_image`: two bindings of one
# slot that share no fragment are what
# `test_foreign_typename_reads_as_none` reads one against the other's
# result for.
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
get_image_or_link = get_attachment.bind(attachment=(image_caption, link_href))
get_identity = get_attachment.bind(attachment=attachment_identity)
