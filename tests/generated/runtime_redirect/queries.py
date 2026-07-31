from tests.generated.runtime_redirect.gql.api import api_gql

ping = api_gql(
    """
    query Ping {
        ping
    }
    """
)
