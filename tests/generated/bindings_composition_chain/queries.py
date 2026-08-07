from tests.generated.bindings_composition_chain.gql.api import api_gql

leaf_parts = api_gql(
    """
    fragment LeafParts on ImageAttachment {
        url
    }
    """
)

middle_parts = api_gql(
    """
    fragment MiddleParts on ImageAttachment {
        caption
        ...LeafParts
    }
    """
)

root_parts = api_gql(
    """
    fragment RootParts on ImageAttachment {
        altText
        ...MiddleParts
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

bound = get_attachment.bind(attachment=root_parts)
