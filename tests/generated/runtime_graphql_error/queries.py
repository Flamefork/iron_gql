from tests.generated.runtime_graphql_error.gql.api import api_gql

fail = api_gql(
    """
    query Fail {
        fail
    }
    """
)
