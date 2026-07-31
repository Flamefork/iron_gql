from tests.generated.fragments_inline.gql.api import api_gql

get_viewer = api_gql(
    """
    query GetViewer {
        viewer {
            id
            ... {
                name
                email
            }
        }
    }
    """
)
