from tests.generated.bindings_overlap.gql.api import api_gql

image_caption = api_gql(
    """
    fragment ImageCaption on ImageAttachment {
        caption
    }
    """
)

image_size = api_gql(
    """
    fragment ImageSize on ImageAttachment {
        width
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

# Two fragments covering the SAME runtime type in one slot: each reads its
# own slice independently, so overlap is legal (see the slot-read spec).
both = get_attachment.bind(attachment=(image_caption, image_size))
