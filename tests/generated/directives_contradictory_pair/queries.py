from tests.generated.directives_contradictory_pair.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($flag: Boolean!) {
        user {
            id
            name @include(if: $flag) @skip(if: $flag)
        }
    }
    """
)
