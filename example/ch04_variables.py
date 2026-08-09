from example.gql.api import FindUserByEmail
from example.gql.api import FindUserById
from example.gql.api import api_gql


async def fetch_profile(user_id: str, *, with_email: bool, skip_phone: bool) -> None:
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
    """).execute(id=user_id, with_email=with_email, skip_phone=skip_phone)

    if result.user is None:
        return

    print(f"Profile: {result.user.name}")
    if result.user.email is not None:
        print(f"  email: {result.user.email}")
    if result.user.phone is not None:
        print(f"  phone: {result.user.phone}")


async def find_user(by: FindUserById | FindUserByEmail) -> None:
    result = await api_gql("""
        query FindUser($by: FindUserBy!) {
            findUser(by: $by) {
                id
                name
                email
            }
        }
    """).execute(by=by)

    if result.find_user is not None:
        print(f"Found: {result.find_user.name}")
