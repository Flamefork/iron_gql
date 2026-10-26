from tests.generated.model_base_sync.gql.api import api_gql

get_post = api_gql("query GetPost { post { id postTitle } }")
get_attachment = api_gql("""
    query GetAttachment {
        post {
            attachment {
                __typename
                ... on ImageAttachment { url }
                ... on LinkAttachment { href }
            }
        }
    }
""")
get_slotted = api_gql("""
    query GetSlotted { post { attachment @slot { __typename } } }
""")
image_url = api_gql("fragment ImageUrl on ImageAttachment { url }")
get_slotted_image = get_slotted.bind(attachment=image_url)
counter = api_gql("subscription Counter { counter }")
