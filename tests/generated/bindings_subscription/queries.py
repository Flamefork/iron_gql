from tests.generated.bindings_subscription.gql.api import api_gql

image_url = api_gql(
    """
    fragment ImageUrl on ImageAttachment {
        url
    }
    """
)

watch_attachment = api_gql(
    """
    subscription WatchAttachment($id: ID!) {
        attachmentChanged(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
    """
)

bound = watch_attachment.bind(attachment=image_url)
