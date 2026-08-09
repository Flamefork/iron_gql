from example.gql.api import api_gql


async def watch_new_posts(user_id: str) -> None:
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

    async with subscription.execute(user_id=user_id) as stream:
        async for event in stream:
            post = event.post_added
            print(f"New post: {post.title} by {post.author.name}")
