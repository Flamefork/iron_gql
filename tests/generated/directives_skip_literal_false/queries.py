from tests.generated.directives_skip_literal_false.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser {
        user {
            id
            name @skip(if: false)
        }
    }
    """
)
