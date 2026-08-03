from tests.generated.directives_inline_literal_false.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser {
        user {
            id
            ... @include(if: false) { name }
        }
    }
    """
)
