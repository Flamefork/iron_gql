from example.gql.api import api_gql


async def show_attachment(post_id: str, width: int) -> None:
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

    image_thumbnail = api_gql("""
        fragment ImageThumbnail on ImageAttachment {
            thumbnail(width: $width)
        }
    """)

    either = get_post_attachment.bind(attachment=[image_url, link_url])
    found = await either.execute(id=post_id)

    if found.post is not None:
        image = image_url.read(found.post.attachment)
        if image is not None:
            print(f"Image: {image.url}")

        link = link_url.read(found.post.attachment)
        if link is not None:
            print(f"Link: {link.href}")

    preview = get_post_attachment.bind(attachment=image_thumbnail)
    scaled = await preview.with_args(width=width).execute(id=post_id)

    if scaled.post is not None:
        thumbnail = image_thumbnail.read(scaled.post.attachment)
        if thumbnail is not None:
            print(f"Thumbnail: {thumbnail.thumbnail}")
