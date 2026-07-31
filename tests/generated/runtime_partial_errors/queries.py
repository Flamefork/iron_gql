from tests.generated.runtime_partial_errors.gql.api import api_gql

ok = api_gql(
    """
    query Ok {
        ok
    }
    """
)
