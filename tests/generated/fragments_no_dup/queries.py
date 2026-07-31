from tests.generated.fragments_no_dup.gql.api import api_gql

get_user = api_gql(
    """
    fragment UserFields on User {
        id
        name
    }

    query GetUser($id: ID!) {
        user(id: $id) {
            ...UserFields
        }
    }
    """
)
