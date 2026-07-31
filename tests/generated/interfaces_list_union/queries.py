from tests.generated.interfaces_list_union.gql.api import api_gql

get_nodes = api_gql(
    """
    query GetNodes {
        nodes {
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
