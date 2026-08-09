from example.gql.api import CreateUserInput
from example.gql.api import api_gql


async def create_user(name: str, email: str) -> str:
    result = await api_gql("""
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
