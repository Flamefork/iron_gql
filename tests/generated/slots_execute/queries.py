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
