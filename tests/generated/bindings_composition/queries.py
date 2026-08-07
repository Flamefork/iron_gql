from tests.generated.bindings_composition.gql.api import api_gql

base_parts = api_gql(
    """
    fragment BaseParts on ImageAttachment {
        url
    }
    """
)

image_parts = api_gql(
    """
    fragment ImageParts on ImageAttachment {
        ...BaseParts
    }
    """
)

foreign_parts = api_gql(
    """
    fragment ForeignParts on ImageAttachment {
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

bound = get_attachment.bind(attachment=image_parts)
# A second bind of the same template, so `foreign_parts` is genuinely
# bind-reachable (gets a real typed handle) while still being foreign to
# `bound`'s own closure -- an orphan fragment (bound nowhere) never
# becomes a handle at all (`parser.bind_closures` drops it), so
# this is the only way to test "outside this binding's closure" rather
# than "outside every binding".
elsewhere = get_attachment.bind(attachment=foreign_parts)
