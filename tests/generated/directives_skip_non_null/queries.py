from tests.generated.directives_skip_non_null.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($skipName: Boolean!) {
        user {
            id
            name @skip(if: $skipName)
        }
    }
    """
)
