from tests.generated.interfaces_union_iface_fragment.gql.api import api_gql

get_actor = api_gql(
    """
    query GetActor($id: ID!) {
        actor(id: $id) {
            __typename
            ... on Node {
                id
            }
            ... on User {
                name
            }
            ... on Admin {
                permissions
            }
        }
    }
    """
)
