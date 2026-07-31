from tests.generated.interfaces_named_fragment.gql.api import api_gql

user_fields = api_gql(
    """
    fragment UserFields on User {
        name
    }
    """
)

get_node = api_gql(
    """
    query GetNode($id: ID!) {
        node(id: $id) {
            __typename
            id
            ...UserFields
        }
    }
    """
)
