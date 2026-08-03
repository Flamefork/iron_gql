from tests.generated.fragment_handles.gql.api import api_gql

user_fields = api_gql(
    """
    fragment UserFields on User {
        id
        name
    }
    """
)

node_fields = api_gql(
    """
    fragment NodeFields on Node {
        __typename
        id
        ... on Admin { permissions }
    }
    """
)

combined = api_gql(
    """
    fragment ViewerFields on User {
        name
    }

    query GetViewer {
        viewer {
            id
            ...ViewerFields
        }
    }
    """
)

with_slot = api_gql(
    """
    query WithSlot($id: ID!) {
        node(id: $id) @slot { __typename }
    }
    """
)
