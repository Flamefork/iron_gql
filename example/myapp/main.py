import asyncio

from myapp.gql.api import CreateUserInput, FindUserByEmail, FindUserById, SearchResultSearchPost, SearchResultSearchUser, api_gql


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

if __name__ == "__main__":
    asyncio.run(main())
