from tests.generated.bindings_wire_shape.gql.api import api_gql

get_attachment = api_gql(
    """
    query GetAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
            preview @slot { __typename }
        }
    }
    """
)

image_parts = api_gql(
    """
    fragment ImageParts on ImageAttachment {
        url(payload: $payload)
    }
    """
)

image_files = api_gql(
    """
    fragment ImageFiles on ImageAttachment {
        url(files: $files)
    }
    """
)
