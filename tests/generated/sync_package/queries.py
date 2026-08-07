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

# Finding F12: the renderer emits a distinct sync path for bindings (the
# branch's real-world consumer is a sync package), but nothing calls
# execute() on one -- this template/fragment/bind exercises exactly that.
get_user_with_manager = api_gql(
    """
    query GetUserWithManager($id: ID!) {
        user(id: $id) {
            id
            manager @slot { __typename }
        }
    }
    """
)

manager_name = api_gql(
    """
    fragment ManagerName on User {
        name
    }
    """
)

bound_user_with_manager = get_user_with_manager.bind(manager=manager_name)
