from tests.generated.directives_include_inline_fragment.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($withDetails: Boolean!) {
        user {
            id
            ... @include(if: $withDetails) {
                name
                email
            }
        }
    }
    """
)
