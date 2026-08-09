from pathlib import Path

from example.gql.api import api_gql
from iron_gql import FileVar


async def fetch_post(post_id: str) -> None:
    result = await api_gql("""
        query GetPost($id: ID!) {
            post(id: $id) {
                id
                title
                createdAt
            }
        }
    """).execute(id=post_id)

    if result.post is not None:
        print(f"{result.post.title}, created {result.post.created_at:%Y-%m-%d}")


async def upload_avatar(user_id: str, path: Path) -> None:
    with path.open("rb") as file:
        result = await api_gql("""
            mutation UploadAvatar($userId: ID!, $file: Upload!) {
                uploadAvatar(userId: $userId, file: $file) {
                    id
                    name
                }
            }
        """).execute(user_id=user_id, file=FileVar(file, filename=path.name))

    print(f"Avatar set for {result.upload_avatar.name}")
