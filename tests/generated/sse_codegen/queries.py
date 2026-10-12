from tests.generated.sse_codegen.gql.api import api_gql

events = api_gql(
    """
    subscription Events($channel: String!) {
        events(channel: $channel) {
            id
            message
        }
    }
    """
)
