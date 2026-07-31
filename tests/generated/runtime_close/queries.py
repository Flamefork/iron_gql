from tests.generated.runtime_close.gql.api import api_gql

ping = api_gql(
    """
    query Ping {
        ping
    }
    """
)
