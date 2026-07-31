from tests.generated.runtime_crud.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($id: ID!) {
        user(id: $id) {
            id
            name
        }
    }
    """
)

update_user = api_gql(
    """
    mutation UpdateUser($input: UpdateUserInput!) {
        updateUser(input: $input) {
            id
            name
        }
    }
    """
)
