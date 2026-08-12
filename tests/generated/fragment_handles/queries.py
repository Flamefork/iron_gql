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

# Both fragments are typed definitions because the package holds a template at all;
# these two binds are here for the bound operations the tests below read
# through, not to make the definitions exist.
with_user_fields = with_slot.bind(node=user_fields)
with_node_fields = with_slot.bind(node=node_fields)
