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

# A fragment becomes a handle only once some bind reaches it; these two
# binds are what make `user_fields`/`node_fields` handles for the tests
# below, not the mere existence of a slot they are spread-compatible with.
with_user_fields = with_slot.bind(node=user_fields)
with_node_fields = with_slot.bind(node=node_fields)
