from tests.generated.fragments_exec_source.gql.api import api_gql

user_fragment = api_gql(
    """
    fragment UserFields on User {
        id
        name
    }
    """
)

get_user = api_gql(
    """
    query GetUser($id: ID!) {
        user(id: $id) {
            ...UserFields
        }
    }
    """
)
