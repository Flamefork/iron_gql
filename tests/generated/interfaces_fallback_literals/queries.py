from tests.generated.interfaces_fallback_literals.gql.api import api_gql

get_post = api_gql(
    """
    query GetPost($id: ID!) {
        node(id: $id) {
            __typename
            ... on Post {
                title
            }
        }
    }
    """
)

get_comment = api_gql(
    """
    query GetComment($id: ID!) {
        node(id: $id) {
            __typename
            ... on Comment {
                body
            }
        }
    }
    """
)
