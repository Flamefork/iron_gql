from tests.generated.bindings_fragment_vars.gql.api import api_gql

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

image_parts = api_gql(
    """
    fragment ImageParts on ImageAttachment {
        url(width: $width, size: $size)
    }
    """
)

bound = get_attachment.bind(attachment=image_parts)
