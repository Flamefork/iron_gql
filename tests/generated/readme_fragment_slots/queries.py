from tests.generated.readme_fragment_slots.gql.api import api_gql

get_post_attachment = api_gql("""
    query GetPostAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
""")

image_url = api_gql("""
    fragment ImageUrl on ImageAttachment {
        url
    }
""")

link_url = api_gql("""
    fragment LinkUrl on LinkAttachment {
        href
    }
""")

get_post_attachment_image = get_post_attachment.bind(attachment=image_url)
get_post_attachment_link = get_post_attachment.bind(attachment=link_url)

image_caption = api_gql("""
    fragment ImageCaption on ImageAttachment {
        caption
    }
""")

link_summary = api_gql("""
    fragment LinkSummary on LinkAttachment {
        href
    }
""")

get_post_attachment_any = get_post_attachment.bind(
    attachment=(image_caption, link_summary)
)

image_thumbnail = api_gql("""
    fragment ImageThumbnail on ImageAttachment {
        thumbnail(width: $width)
    }
""")
