from tests.generated.shape_names_one_level.gql.api import api_gql

q1 = api_gql("query Q1 { feed { author { posts { id } } } }")
q2 = api_gql("query Q2 { feed { author { posts { title } } } }")
