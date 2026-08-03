from tests.generated.slots_isolation.gql.api import api_gql

get_board = api_gql(
    """
    query GetBoard($id: ID!) {
        board(id: $id) @slot {
            __typename
            owner { who: fullName }
            cards { title }
            activity {
                __typename
                ... on Comment { body author { who: fullName } }
                ... on Move { fromColumn author { id } }
            }
        }
    }
    """
)

ping_board = api_gql(
    """
    query PingBoard($id: ID!) {
        board(id: $id) @slot { __typename }
    }
    """
)

ping_main = api_gql(
    """
    query PingMain($id: ID!) {
        main: board(id: $id) @slot { __typename }
    }
    """
)

merged_board = api_gql(
    """
    query MergedBoard($id: ID!) {
        merged: board(id: $id) { __typename }
        merged: board(id: $id) @slot { __typename }
    }
    """
)
