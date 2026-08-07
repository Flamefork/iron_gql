import asyncio

from example.gql.api import CreateUserInput
from example.gql.api import FindUserByEmail
from example.gql.api import FindUserById
from example.gql.api import PostWithAuthorIdTitleTypename
from example.gql.api import UserWithIdNameRoleTypename
from example.gql.api import api_gql


# One demonstration per paragraph, as everywhere else in this file -- this
# one gets a name of its own because it is the longest, not because it is
# shared: the queries still sit next to the code that uses them.
async def demo_fragment_slots() -> None:
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

    result = await get_post_attachment.bind(attachment=image_url).execute(id="1")

    if result.post is not None:
        image = image_url.read(result.post.attachment)
        if image is not None:
            print(f"Attachment image: {image.url}")

    result = await get_post_attachment.bind(attachment=link_url).execute(id="2")

    if result.post is not None:
        link = link_url.read(result.post.attachment)
        if link is not None:
            print(f"Attachment link: {link.href}")


async def main():
    result = await api_gql("""
        query GetUser($id: ID!) {
            user(id: $id) {
                ...UserFields
                posts { id title }
            }
        }

        fragment UserFields on User {
            id
            name
            email
            phone
            role
        }
    """).execute(id="1")

    if result.user:
        print(f"{result.user.name} ({result.user.email}), role: {result.user.role}")
        for post in result.user.posts:
            print(f"  - {post.title}")

    result = await api_gql("""
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

    print(f"Created: {result.create_user.name} (id={result.create_user.id})")

    result = await api_gql("""
        query Search($query: String!) {
            search(query: $query) {
                __typename
                ... on User {
                    id
                    name
                    role
                }
                ... on Post {
                    id
                    title
                    author { name }
                }
            }
        }
    """).execute(query="python")

    for item in result.search:
        match item:
            case UserWithIdNameRoleTypename():
                print(f"User: {item.name}, role: {item.role}")
            case PostWithAuthorIdTitleTypename():
                print(f"Post: {item.title} by {item.author.name}")

    result = await api_gql("""
        query GetProfile($id: ID!, $withEmail: Boolean!, $skipPhone: Boolean!) {
            user(id: $id) {
                id
                name
                email @include(if: $withEmail)
                phone @skip(if: $skipPhone)
                role
            }
        }
    """).execute(id="1", with_email=True, skip_phone=False)

    if result.user:
        print(f"Profile: {result.user.name}")
        if result.user.email is not None:
            print(f"  email: {result.user.email}")
        if result.user.phone is not None:
            print(f"  phone: {result.user.phone}")

    result = await api_gql("""
        query FindUser($by: FindUserBy!) {
            findUser(by: $by) {
                id
                name
                email
            }
        }
    """).execute(by=FindUserById(id="1"))

    if result.find_user:
        print(f"Found by ID: {result.find_user.name}")

    result = await api_gql("""
        query FindUser($by: FindUserBy!) {
            findUser(by: $by) {
                id
                name
                email
            }
        }
    """).execute(by=FindUserByEmail(email="alice@example.com"))

    if result.find_user:
        print(f"Found by email: {result.find_user.name}")

    await demo_fragment_slots()

    subscription = api_gql("""
        subscription PostAdded($userId: ID!) {
            postAdded(userId: $userId) {
                id
                title
                body
                author { name }
            }
        }
    """)

    async with subscription.execute(user_id="1") as stream:
        async for event in stream:
            post = event.post_added
            print(f"New post: {post.title} by {post.author.name}")


if __name__ == "__main__":
    asyncio.run(main())
