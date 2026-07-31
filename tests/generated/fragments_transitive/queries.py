from tests.generated.fragments_transitive.gql.api import api_gql

fragment_c = api_gql(
    """
    fragment RoleFields on User {
        role
    }
    """
)

fragment_b = api_gql(
    """
    fragment ContactFields on User {
        email
        ...RoleFields
    }
    """
)

fragment_a = api_gql(
    """
    fragment UserFields on User {
        id
        name
        ...ContactFields
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
