from tests.generated.operation_options_sync.gql.api import api_gql

patient = api_gql("query Patient { ping }").with_timeout(60)
ping = api_gql("query Ping { ping }")
get_user = api_gql("query GetUser($id: ID!) { user(id: $id) { id } }")
touch = api_gql("mutation Touch { touch }")
counter = api_gql("subscription Counter { counter }")
get_node = api_gql("query GetNode { node @slot { __typename } }")
thumbnail = api_gql("fragment Thumbnail on Node { thumbnail(width: $width) }")
