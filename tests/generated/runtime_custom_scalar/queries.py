from tests.generated.runtime_custom_scalar.gql.api import api_gql

get_events = api_gql(
    """
    query GetEvents($since: DateTime!) {
        events(since: $since) {
            name
            startedAt
        }
    }
    """
)
