from tests.generated.directives_include_list.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($withTags: Boolean!) {
        user {
            id
            tags @include(if: $withTags)
        }
    }
    """
)
