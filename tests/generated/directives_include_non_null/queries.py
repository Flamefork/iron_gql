from tests.generated.directives_include_non_null.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($withName: Boolean!) {
        user {
            id
            name @include(if: $withName)
        }
    }
    """
)
