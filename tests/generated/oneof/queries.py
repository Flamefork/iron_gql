from tests.generated.oneof.gql.api import api_gql

search = api_gql(
    """
    mutation Search($criteria: SearchCriteria!) {
        search(criteria: $criteria)
    }
    """
)

update = api_gql(
    """
    mutation Update($input: UpdateAction!) {
        update(input: $input)
    }
    """
)

do_search = api_gql(
    """
    mutation DoSearch($input: WrapperInput!) {
        doSearch(input: $input)
    }
    """
)

act = api_gql(
    """
    mutation Act($input: SingleChoice!) {
        act(input: $input)
    }
    """
)

do_filter = api_gql(
    """
    mutation Filter($by: FilterBy!) {
        filter(by: $by)
    }
    """
)

list_search = api_gql(
    """
    mutation ListSearch($by: SearchBy!) {
        searchBy(by: $by)
    }
    """
)

act_nested = api_gql(
    """
    mutation ActNested($input: OuterChoice!) {
        actNested(input: $input)
    }
    """
)
