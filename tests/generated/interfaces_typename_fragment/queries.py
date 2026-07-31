from tests.generated.interfaces_typename_fragment.gql.api import api_gql

node_base = api_gql(
    """
    fragment NodeBase on Node {
        __typename
        id
    }
    """
)

get_node = api_gql(
    """
    query GetNode($id: ID!) {
        node(id: $id) {
            ...NodeBase
            ... on User {
                name
            }
            ... on Post {
                title
            }
        }
    }
    """
)
