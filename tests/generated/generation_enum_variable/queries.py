from tests.generated.generation_enum_variable.gql.api import api_gql

search = api_gql(
    """
    query Search($status: Status!) {
        search(status: $status)
    }
    """
)
