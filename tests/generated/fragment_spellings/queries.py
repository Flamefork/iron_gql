from tests.generated.fragment_spellings.gql.api import api_gql

find = api_gql("query Find { node @slot { __typename } }")

compact = api_gql("fragment Fields on Node { id name }")

spelled_out = api_gql(
    """
    # The same fragment, laid out and commented.
    fragment Fields on Node {
        id
        name
    }
    """
)

by_compact = find.bind(node=compact)
by_spelled_out = find.bind(node=spelled_out)
