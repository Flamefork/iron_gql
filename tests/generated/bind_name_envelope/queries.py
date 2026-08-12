from tests.generated.bind_name_envelope.gql.api import api_gql

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

# Slots two fragments are compatible with: `bind()` is a set of
# `@overload` stubs over an erased implementation.
#
# `$cast` is the same axis in the other parameter namespace the generator
# writes: a template's variables become `execute()`'s keywords, and the
# bound base's `execute` reads a name of the renderer's own to reconcile
# the result type it promises with the one class it validates against.
overloaded = api_gql(
    """
    query Overloaded($cast: ID!) {
        post(id: $cast) {
            id
            cls: attachment @slot { __typename }
            bind: attachment @slot { __typename }
            pydantic: attachment @slot { __typename }
        }
    }
    """
)

# A slot of a type no fragment in the package is defined on: the empty
# call is the only one `bind()` accepts, so it is written as one plain
# signature -- the same names, the other end of the axis.
inline = api_gql(
    """
    query Inline($id: ID!) {
        post(id: $id) {
            id
            cls: author @slot { __typename }
        }
    }
    """
)

overloaded_cls = overloaded.bind(cls=image_parts)
overloaded_pair = overloaded.bind(pydantic=link_parts, bind=image_parts)
