from tests.generated.slots_subscription.gql.api import api_gql

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

watch_image = watch_attachment.bind(attachment=image_url)
