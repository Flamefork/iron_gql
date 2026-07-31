from tests.generated.directives_shared_variable.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($flag: Boolean!) {
        user {
            id
            email @include(if: $flag)
            phone @skip(if: $flag)
        }
    }
    """
)
