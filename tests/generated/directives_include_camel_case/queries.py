from tests.generated.directives_include_camel_case.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($withName: Boolean!) {
        user {
            id
            firstName @include(if: $withName)
        }
    }
    """
)
