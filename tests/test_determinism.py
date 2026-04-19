from tests.conftest import ProjectBuilder


def test_regeneration_is_byte_stable(test_project: ProjectBuilder):
    # For codegen, the worst kind of regression is correct-but-unstable output
    # (names flicker, field ordering drifts). This test pins the full module.
    test_project.prepare(
        schema="""
            interface Node {
                id: ID!
            }

            type User implements Node {
                id: ID!
                name: String!
                posts: [Post!]!
            }

            type Post implements Node {
                id: ID!
                title: String!
                author: User!
            }

            input PostFilter {
                author_id: ID
                titles: [String!]
            }

            input OneOfTarget @oneOf {
                user_id: ID
                post_id: ID
            }

            enum Status {
                ACTIVE
                ARCHIVED
            }

            type Query {
                node(id: ID!): Node
                user(id: ID!): User
                posts(filter: PostFilter, status: Status): [Post!]!
                search(target: OneOfTarget!): Node
            }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        get_user = api_gql('''
            query GetUser($id: ID!) {
                user(id: $id) {
                    id
                    name
                    posts { id title }
                }
            }
        ''')

        list_posts = api_gql('''
            query ListPosts($filter: PostFilter, $status: Status) {
                posts(filter: $filter, status: $status) {
                    id
                    title
                    author { id name }
                }
            }
        ''')

        find_node = api_gql('''
            query FindNode($id: ID!) {
                node(id: $id) {
                    __typename
                    ... on User { id name }
                    ... on Post { id title }
                }
            }
        ''')

        search = api_gql('''
            query Search($target: OneOfTarget!) {
                search(target: $target) {
                    __typename
                    id
                }
            }
        ''')
        """,
    )

    assert test_project.generate() is True
    first = (test_project.root / "sample_app/gql/api.py").read_text()

    assert test_project.generate() is False
    second = (test_project.root / "sample_app/gql/api.py").read_text()
    assert first == second
