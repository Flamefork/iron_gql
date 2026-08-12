from tests.generated.fragment_factory_typing.gql.api import api_gql

get_attachment = api_gql("""
    query GetAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
""")

sized_image = api_gql("""
    fragment SizedImage on ImageAttachment {
        thumbnail(width: $width)
    }
""")

image_caption = api_gql("""
    fragment ImageCaption on ImageAttachment {
        caption
    }
""")

wrapper = api_gql("""
    fragment Wrapper on ImageAttachment {
        caption
        ...SizedImage
    }
""")

tuple_bound = get_attachment.bind(
    attachment=(image_caption, sized_image.with_args(width=64))
)
