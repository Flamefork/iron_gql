from tests.generated.bindings_disjoint.gql.api import api_gql

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
        url
    }
    """
)

link_parts = api_gql(
    """
    fragment LinkParts on LinkAttachment {
        href
    }
    """
)

other_parts = api_gql(
    """
    fragment OtherParts on ImageAttachment {
        url
    }
    """
)

both = get_attachment.bind(attachment=[image_parts, link_parts])
foreign = get_attachment.bind(attachment=other_parts)
