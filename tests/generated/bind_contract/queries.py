from tests.generated.bind_contract.gql.api import api_gql

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

nothing = get_attachment.bind()
one = get_attachment.bind(attachment=image_parts)
two = get_attachment.bind(attachment=[image_parts, link_parts])
both_slots = get_attachment.bind(attachment=image_parts, preview=link_parts)
