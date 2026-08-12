from tests.generated.bindings_fragment_var_nullability.gql.api import api_gql

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
        url(width: $width, height: $height, pad: $pad, slots: $slots)
    }
    """
)
