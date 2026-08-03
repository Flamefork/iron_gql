from tests.generated.sync_package.gql.api import api_gql

get_user = api_gql(
    """
    query GetUser($id: ID!) {
        user(id: $id) {
            id
            name
        }
    }
    """
)

rename_user = api_gql(
    """
    mutation RenameUser($id: ID!, $name: String!) {
        renameUser(id: $id, name: $name) {
            id
            name
        }
    }
    """
)

user_renamed = api_gql(
    """
    subscription UserRenamed($id: ID!) {
        userRenamed(id: $id) {
            id
            name
        }
    }
    """
)
