from tests.generated.directives_include_skip_same_field.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($show: Boolean!, $hide: Boolean!) {
        user {
            id
            name @include(if: $show) @skip(if: $hide)
        }
    }
    """
)
