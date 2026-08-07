from tests.generated.slots_basic.gql.api import api_gql

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

attach = api_gql(
    """
    mutation Attach($id: ID!) {
        attach(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
    """
)

image_url = api_gql(
    """
    fragment ImageUrl on ImageAttachment {
        url
    }
    """
)

attach_image = attach.bind(attachment=image_url)
