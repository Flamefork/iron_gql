from tests.generated.runtime_oneof.gql.api import api_gql

search = api_gql(
    """
    query Search($criteria: SearchCriteria!) {
        search(criteria: $criteria)
    }
    """
)
