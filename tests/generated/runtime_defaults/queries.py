from tests.generated.runtime_defaults.gql.api import api_gql

get_posts = api_gql(
    """
    query GetPosts($limit: Int = 5) {
        posts(limit: $limit)
    }
    """
)
