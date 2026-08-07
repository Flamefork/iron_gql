from example.gql.api_sync import CreateUserInput
from example.gql.api_sync import api_sync_gql


def main():
    result = api_sync_gql("""
        query GetUser($id: ID!) {
            user(id: $id) {
                id
                name
                email
                role
            }
        }
    """).execute(id="1")

    if result.user:
        print(f"{result.user.name} ({result.user.email}), role: {result.user.role}")

    new_user = api_sync_gql("""
        mutation CreateUser($input: CreateUserInput!) {
            createUser(input: $input) {
                id
                name
                email
                role
            }
        }
    """).execute(
        input=CreateUserInput(name="Alice", email="alice@example.com", role="ADMIN")
    )

    print(f"Created: {new_user.create_user.name} (id={new_user.create_user.id})")

    get_post_attachment = api_sync_gql("""
        query GetPostAttachment($id: ID!) {
            post(id: $id) {
                id
                attachment @slot { __typename }
            }
        }
    """)

    image_url = api_sync_gql("""
        fragment ImageUrl on ImageAttachment {
            url
        }
    """)

    link_url = api_sync_gql("""
        fragment LinkUrl on LinkAttachment {
            href
        }
    """)

    result = get_post_attachment.bind(attachment=image_url).execute(id="1")

    if result.post is not None:
        image = image_url.read(result.post.attachment)
        if image is not None:
            print(f"Attachment image: {image.url}")

    result = get_post_attachment.bind(attachment=link_url).execute(id="2")

    if result.post is not None:
        link = link_url.read(result.post.attachment)
        if link is not None:
            print(f"Attachment link: {link.href}")

    subscription = api_sync_gql("""
        subscription PostAdded($userId: ID!) {
            postAdded(userId: $userId) {
                id
                title
                body
                author { name }
            }
        }
    """)

    with subscription.execute(user_id="1") as stream:
        for event in stream:
            post = event.post_added
            print(f"New post: {post.title} by {post.author.name}")


if __name__ == "__main__":
    main()
