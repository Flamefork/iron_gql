from tests.generated.interfaces_nested.gql.api import api_gql

get_node = api_gql(
    """
    query GetNode($id: ID!) {
        node(id: $id) {
            __typename
            id
            child {
                id
            }
        }
    }
    """
)
