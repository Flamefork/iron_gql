from tests.generated.directives_include_literal_true.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser {
        user {
            id
            name @include(if: true)
        }
    }
    """
)
