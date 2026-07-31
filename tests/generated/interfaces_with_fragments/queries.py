from tests.generated.interfaces_with_fragments.gql.api import api_gql

get_node = api_gql(
    """
    query GetNode($id: ID!) {
        node(id: $id) {
            __typename
            id
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
