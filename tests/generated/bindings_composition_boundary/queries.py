from tests.generated.bindings_composition_boundary.gql.api import api_gql

node_id = api_gql(
    """
    fragment NodeId on Node {
        id
    }
    """
)

image_parts = api_gql(
    """
    fragment ImageParts on ImageAttachment {
        url
        ...NodeId
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

bound = get_attachment.bind(attachment=image_parts)
