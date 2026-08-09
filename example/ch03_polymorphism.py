from example.gql.api import PostWithAuthorIdTitleTypename
from example.gql.api import PostWithIdTitleTypename
from example.gql.api import UserWithIdNameRoleTypename
from example.gql.api import UserWithIdNameTypename
from example.gql.api import api_gql


async def search_anything(query: str) -> None:
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
    """).execute(query=query)

    for item in result.search:
        match item:
            case UserWithIdNameRoleTypename():
                print(f"User: {item.name}, role: {item.role}")
            case PostWithAuthorIdTitleTypename():
                print(f"Post: {item.title} by {item.author.name}")


async def fetch_node(node_id: str) -> None:
    result = await api_gql("""
        query GetNode($id: ID!) {
            node(id: $id) {
                __typename
                id
                ... on User { name }
                ... on Post { title }
            }
        }
    """).execute(id=node_id)

    match result.node:
        case UserWithIdNameTypename():
            print(f"User node: {result.node.name}")
        case PostWithIdTitleTypename():
            print(f"Post node: {result.node.title}")
        case None:
            print(f"No node {node_id}")
