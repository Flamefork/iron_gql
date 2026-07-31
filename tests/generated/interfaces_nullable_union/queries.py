from tests.generated.interfaces_nullable_union.gql.api import api_gql

get_node = api_gql(
    """
    query GetNode($id: ID!) {
        node(id: $id) {
            __typename
            ... on User {
                id
                name
            }
            ... on Admin {
                id
                permissions
            }
        }
    }
    """
)
