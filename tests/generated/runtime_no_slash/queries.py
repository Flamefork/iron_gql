from tests.generated.runtime_no_slash.gql.api import api_gql

ping = api_gql(
    """
    query Ping {
        ping
    }
    """
)
