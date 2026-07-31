from tests.generated.generation_nested_inputs.gql.api import api_gql

update_user = api_gql(
    """
    mutation UpdateUser($input: UpdateUserInput!) {
        updateUser(input: $input)
    }
    """
)
