from tests.generated.interfaces_union_partial.gql.api import api_gql

search = api_gql(
    """
    query Search {
        search {
            __typename
            ... on User {
                name
            }
        }
    }
    """
)
