from tests.generated.runtime_null_elements.gql.api import api_gql

numbers = api_gql(
    """
    query Numbers {
        numbers1
        numbers2
    }
    """
)
