from tests.generated.directives_complementary_conjunctions.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($a: Boolean!, $b: Boolean!) {
        user {
            id
            ... @include(if: $a) { ... @include(if: $b) { name } }
            ... @skip(if: $a) { ... @skip(if: $b) { name } }
        }
    }
    """
)
