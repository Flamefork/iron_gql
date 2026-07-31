from tests.generated.directives_include_nested_object.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($withAddress: Boolean!) {
        user {
            id
            address @include(if: $withAddress) {
                city
                zip
            }
        }
    }
    """
)
