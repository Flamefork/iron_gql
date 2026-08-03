from tests.generated.slots_lists.gql.api import api_gql

get_events = api_gql(
    """
    query GetEvents($id: ID!) {
        board(id: $id) @slot {
            __typename
            events { __typename }
        }
    }
    """
)

get_cards = api_gql(
    """
    query GetCards($id: ID!) {
        board(id: $id) {
            cards @slot { __typename }
        }
    }
    """
)

activity_texts = api_gql(
    """
    fragment ActivityTexts on Board {
        events {
            __typename
            ... on Comment { body }
            ... on Move { fromColumn }
        }
    }
    """
)

card_title = api_gql(
    """
    fragment CardTitle on Card {
        title
    }
    """
)
