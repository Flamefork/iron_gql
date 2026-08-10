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

# Several bindings: one `@overload` each over the shared implementation.
overloaded = api_gql(
    """
    query Overloaded($id: ID!) {
        post(id: $id) {
            id
            cls: attachment @slot { __typename }
            msg: attachment @slot { __typename }
            key: attachment @slot { __typename }
            bind: attachment @slot { __typename }
            fragments: attachment @slot { __typename }
            dispatch: attachment @slot { __typename }
            pydantic: attachment @slot { __typename }
            runtime: attachment @slot { __typename }
            sequence: attachment @slot { __typename }
        }
    }
    """
)

# A single binding, where the renderer has to write the second signature
# itself -- the same names, the other end of the axis.
inline = api_gql(
    """
    query Inline($id: ID!) {
        post(id: $id) {
            id
            cls: attachment @slot { __typename }
        }
    }
    """
)

overloaded_cls = overloaded.bind(cls=image_parts)
overloaded_pair = overloaded.bind(pydantic=link_parts, runtime=image_parts)
inline_cls = inline.bind(cls=image_parts)
