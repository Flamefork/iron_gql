import asyncio

from example.gql.api import CreateUserInput
from example.gql.api import FindUserByEmail
from example.gql.api import FindUserById
from example.gql.api import SearchResultSearchPost
from example.gql.api import SearchResultSearchUser
from example.gql.api import api_gql


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

    new_user = await api_gql("""
        mutation CreateUser($input: CreateUserInput!) {
            createUser(input: $input) {
                id
                name
                email
                role
            }
        }
    """).execute(
        input=CreateUserInput(name="Alice", email="alice@example.com", role="ADMIN"),
    )
    print(f"Created: {new_user.create_user.name} (id={new_user.create_user.id})")

    results = await api_gql("""
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
    for item in results.search:
        match item:
            case SearchResultSearchUser():
                print(f"User: {item.name}, role: {item.role}")
            case SearchResultSearchPost():
                print(f"Post: {item.title} by {item.author.name}")

    profile = await api_gql("""
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
    if profile.user:
        print(f"Profile: {profile.user.name}")
        if profile.user.email is not None:
            print(f"  email: {profile.user.email}")
        if profile.user.phone is not None:
            print(f"  phone: {profile.user.phone}")

    found = await api_gql("""
        query FindUser($by: FindUserBy!) {
            findUser(by: $by) {
                id
                name
                email
            }
        }
    """).execute(by=FindUserById(id="1"))
    if found.find_user:
        print(f"Found by ID: {found.find_user.name}")

    found = await api_gql("""
        query FindUser($by: FindUserBy!) {
            findUser(by: $by) {
                id
                name
                email
            }
        }
    """).execute(by=FindUserByEmail(email="alice@example.com"))
    if found.find_user:
        print(f"Found by email: {found.find_user.name}")

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
