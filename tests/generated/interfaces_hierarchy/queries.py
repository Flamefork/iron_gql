from tests.generated.interfaces_hierarchy.gql.api import api_gql

get_node = api_gql(
    """
    query GetNode($id: ID!) {
        node(id: $id) {
            __typename
            id
            ... on Entity {
                createdAt
            }
        }
    }
    """
)
