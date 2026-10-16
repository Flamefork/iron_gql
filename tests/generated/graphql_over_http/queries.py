from tests.generated.graphql_over_http.gql.api import api_gql

ping = api_gql("query Ping { ping }")
counter = api_gql("subscription Counter { counter }")
