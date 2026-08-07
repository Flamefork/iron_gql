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

board_id = api_gql(
    """
    fragment BoardId on Board {
        id
    }
    """
)

ping_main_bare = ping_main.bind()
# A template whose only binding is the all-unfilled one renders a single
# `@overload` for `bind()`, which basedpyright always flags
# (reportInconsistentOverload -- it requires 2+ overload variants); this
# second, otherwise-unused binding keeps the overload count at 2 so the
# bare one below stays checkable.
ping_main_typed = ping_main.bind(main=board_id)
merged_board_bare = merged_board.bind()
merged_board_typed = merged_board.bind(merged=board_id)
