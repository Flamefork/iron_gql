from tests.generated.runtime_redirect_no_location.gql.api import api_gql

ping = api_gql("query Ping { ping }")
