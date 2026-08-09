from example.gql.api_sync import CreateUserInput
from example.gql.api_sync import api_sync_gql


def fetch_user(user_id: str) -> None:
    result = api_sync_gql("""
        query GetUser($id: ID!) {
            user(id: $id) {
                id
                name
                email
                role
            }
        }
    """).execute(id=user_id)

    if result.user is not None:
        print(f"{result.user.name} ({result.user.email}), role: {result.user.role}")


def create_user(name: str, email: str) -> str:
    result = api_sync_gql("""
        mutation CreateUser($input: CreateUserInput!) {
            createUser(input: $input) {
                id
                name
                email
                role
            }
        }
    """).execute(input=CreateUserInput(name=name, email=email, role="ADMIN"))

    print(f"Created: {result.create_user.name} (id={result.create_user.id})")
    return result.create_user.id


def watch_new_posts(user_id: str) -> None:
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

    with subscription.execute(user_id=user_id) as stream:
        for event in stream:
            post = event.post_added
            print(f"New post: {post.title} by {post.author.name}")
