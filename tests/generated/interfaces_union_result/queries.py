from tests.generated.interfaces_union_result.gql.api import api_gql

get_node_and_count = api_gql(
    """
    query GetNodeAndCount($id: ID!) {
        node(id: $id) {
            __typename
            ... on User {
                id
                name
            }
            ... on Admin {
                id
                name
                permissions
            }
        }
        count
    }
    """
)
