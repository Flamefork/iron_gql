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
# `foreign_parts` is a typed definition like every fragment of a package
# with a template, and its own combination is enumerated whether or not
# anybody writes it -- so it is foreign to `bound`'s closure while still
# being a real definition, which is what "outside this binding's closure"
# (rather than "outside every binding") needs.
