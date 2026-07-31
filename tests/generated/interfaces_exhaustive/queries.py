from tests.generated.interfaces_exhaustive.gql.api import api_gql

get_node = api_gql(
    """
    query GetNode {
        node {
            __typename
            ... on User {
                id
                name
            }
            ... on Post {
                id
                title
            }
        }
    }
    """
)
