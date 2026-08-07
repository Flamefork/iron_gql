from tests.generated.bindings_two_templates.gql.api import api_gql

image_parts = api_gql(
    """
    fragment ImageParts on ImageAttachment {
        url
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

get_highlight = api_gql(
    """
    query GetHighlight($id: ID!) {
        post(id: $id) {
            id
            highlight @slot { __typename }
        }
    }
    """
)

bound_attachment = get_attachment.bind(attachment=image_parts)
bound_highlight = get_highlight.bind(highlight=image_parts)
