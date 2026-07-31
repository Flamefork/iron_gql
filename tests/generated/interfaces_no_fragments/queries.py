from tests.generated.interfaces_no_fragments.gql.api import api_gql

get_node = api_gql(
    """
    query GetNode($id: ID!) {
        node(id: $id) {
            id
        }
    }
    """
)
