from tests.generated.fragments_scoped.gql.api import api_gql

user_fragment = api_gql(
    """
    fragment UserFields on User {
        id
        name
    }
    """
)

post_fragment = api_gql(
    """
    fragment PostFields on Post {
        id
        title
    }
    """
)

get_user = api_gql(
    """
    query GetUser($id: ID!) {
        user(id: $id) {
            ...UserFields
        }
    }
    """
)

get_post = api_gql(
    """
    query GetPost($id: ID!) {
        post(id: $id) {
            ...PostFields
        }
    }
    """
)
