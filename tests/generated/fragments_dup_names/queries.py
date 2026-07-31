from tests.generated.fragments_dup_names.gql.api import api_gql

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

other_fragment = api_gql(
    """
    fragment UserFields on User {
        id
    }
    """
)
