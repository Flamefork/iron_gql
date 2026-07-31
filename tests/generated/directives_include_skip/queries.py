from tests.generated.directives_include_skip.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($id: ID!, $withEmail: Boolean!, $skipPhone: Boolean!) {
        user(id: $id) {
            name
            email @include(if: $withEmail)
            phone @skip(if: $skipPhone)
        }
    }
    """
)
