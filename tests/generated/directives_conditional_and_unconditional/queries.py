from tests.generated.directives_conditional_and_unconditional.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($withDetails: Boolean!) {
        user {
            id
            name
            ... @include(if: $withDetails) {
                name
            }
        }
    }
    """
)
