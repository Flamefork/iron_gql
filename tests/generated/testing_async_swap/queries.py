from tests.generated.testing_async_swap.gql.api import api_gql

ping = api_gql(
    """
    query Ping {
        ping
    }
    """
)
