from tests.generated.bindings_closure_shape.gql.api import api_gql

node_id = api_gql(
    """
    fragment NodeId on Node {
        id
    }
    """
)

thumb_alt = api_gql(
    """
    fragment ThumbAlt on Thumb {
        alt
    }
    """
)

image_parts = api_gql(
    """
    fragment ImageParts on ImageAttachment {
        url
        ...NodeId
        thumb { ...ThumbAlt }
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

bound = get_attachment.bind(attachment=[image_parts, link_parts])
