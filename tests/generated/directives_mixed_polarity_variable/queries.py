from tests.generated.directives_mixed_polarity_variable.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($a: Boolean!, $b: Boolean!) {
        user {
            id
            ... @include(if: $b) { name }
            ... @include(if: $a) @skip(if: $b) { email }
        }
    }
    """
)
