import pydantic

from example.gql.api import OnImageAttachment
from example.gql.api import api_gql

# Kept private on purpose: nothing outside this module needs the template.
# Every combination of it with a compatible fragment is generated from the
# schema, so `attachment_of` below can bind a fragment it was handed without
# the generator ever seeing that call site.
_post_attachment = api_gql("""
    query PostAttachment($id: ID!) {
        post(id: $id) {
            id
            attachment @slot { __typename }
        }
    }
""")


async def attachment_of[TModel: pydantic.BaseModel, TReads](
    post_id: str, details: OnImageAttachment[TModel, TReads]
) -> TModel | None:
    # The caller's fragment goes in, the caller's own model comes out: the
    # on-type base carries the model through `bind()` into the result, so
    # `read` here is as precise as it would be at a concrete call site.
    bound = _post_attachment.bind(attachment=details)
    result = await bound.execute(id=post_id)
    return details.read(result.post.attachment) if result.post else None


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

    either = get_post_attachment.bind(attachment=(image_url, link_url))
    found = await either.execute(id=post_id)

    if found.post is not None:
        image = image_url.read(found.post.attachment)
        if image is not None:
            print(f"Image: {image.url}")

        link = link_url.read(found.post.attachment)
        if link is not None:
            print(f"Link: {link.href}")

    # `with_args` создаёт application со значениями variables для `bind()`.
    # Definition той же factory читает результат независимо от application.
    preview = get_post_attachment.bind(
        attachment=image_thumbnail.with_args(width=width)
    )
    scaled = await preview.execute(id=post_id)

    if scaled.post is not None:
        thumbnail = image_thumbnail.read(scaled.post.attachment)
        if thumbnail is not None:
            print(f"Thumbnail: {thumbnail.thumbnail}")

    # The same read through infrastructure that never names the fragment.
    through_helper = await attachment_of(post_id, image_url)
    if through_helper is not None:
        print(f"Helper: {through_helper.url}")
