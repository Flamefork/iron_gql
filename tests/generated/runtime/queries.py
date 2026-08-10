from tests.generated.runtime.gql.api import api_gql

ping = api_gql("query Ping { ping }")
fail = api_gql("query Fail { fail }")
ok = api_gql("query Ok { ok }")

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

update_user = api_gql(
    """
    mutation UpdateUser($input: UpdateUserInput!) {
        updateUser(input: $input) {
            id
            name
        }
    }
    """
)

numbers = api_gql(
    """
    query Numbers {
        numbers1
        numbers2
    }
    """
)

get_posts = api_gql(
    """
    query GetPosts($limit: Int = 5) {
        posts(limit: $limit)
    }
    """
)

get_events = api_gql(
    """
    query GetEvents($since: DateTime!) {
        events(since: $since) {
            name
            startedAt
        }
    }
    """
)

search = api_gql(
    """
    query Search($criteria: SearchCriteria!) {
        search(criteria: $criteria)
    }
    """
)

upload_file = api_gql(
    """
    mutation UploadFile($file: Upload!, $label: String!) {
        uploadFile(file: $file, label: $label)
    }
    """
)

upload = api_gql(
    """
    mutation Upload($file: Upload!) {
        uploadFile(file: $file)
    }
    """
)
