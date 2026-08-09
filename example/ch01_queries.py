from example.gql.api import api_gql


async def fetch_user(user_id: str) -> None:
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
    """).execute(id=user_id)

    if result.user is None:
        print(f"No user {user_id}")
        return

    print(f"{result.user.name} ({result.user.email}), role: {result.user.role}")
    for post in result.user.posts:
        print(f"  - {post.title}")


async def fetch_user_as(user_id: str, token: str) -> None:
    query = api_gql("""
        query GetUserName($id: ID!) {
            user(id: $id) {
                name
            }
        }
    """).with_headers({"Authorization": f"Bearer {token}"})

    result = await query.execute(id=user_id)

    if result.user is not None:
        print(result.user.name)
