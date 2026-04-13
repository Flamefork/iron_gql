from pytest_httpserver import HTTPServer

from tests.conftest import ProjectBuilder


def test_dedup_within_query_same_union_type(test_project: ProjectBuilder):
    schema = """
        type Query {
            items: ItemResult!
        }

        type ItemResult {
            primary: Item
            secondary: Item
        }

        union Item = Fruit | Vegetable

        type Fruit {
            name: String!
        }

        type Vegetable {
            name: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetItems {
                items {
                    primary {
                        __typename
                        ... on Fruit { name }
                        ... on Vegetable { name }
                    }
                    secondary {
                        __typename
                        ... on Fruit { name }
                        ... on Vegetable { name }
                    }
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    assert generated.count("class Fruit(GQLModel):") == 1
    assert generated.count("class Vegetable(GQLModel):") == 1


def test_dedup_between_queries(test_project: ProjectBuilder):
    schema = """
        type Query {
            feed: [Post!]!
            post(id: ID!): Post
        }

        type Post {
            id: ID!
            title: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        feed = api_gql(
            '''
            query GetFeed {
                feed { id title }
            }
            '''
        )

        post = api_gql(
            '''
            query GetPost($id: ID!) {
                post(id: $id) { id title }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    assert generated.count("class Post(GQLModel):") == 1
    assert "feed: list[Post]" in generated
    assert "post: Post | None" in generated


def test_different_selection_sets_different_classes(test_project: ProjectBuilder):
    schema = """
        type Query {
            feed: [User!]!
            profile: User
        }

        type User {
            id: ID!
            name: String!
            email: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        feed = api_gql(
            '''
            query GetFeed {
                feed { id name }
            }
            '''
        )

        profile = api_gql(
            '''
            query GetProfile {
                profile { id name email }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    assert "class UserWithIdName(GQLModel):" in generated
    assert "class UserWithEmailIdName(GQLModel):" in generated
    assert "feed: list[UserWithIdName]" in generated
    assert "profile: UserWithEmailIdName | None" in generated


def test_different_nested_selection_sets_hash_collision(test_project: ProjectBuilder):
    schema = """
        type Query {
            a: Parent
            b: Parent
        }

        type Parent {
            child: Child!
        }

        type Child {
            id: ID!
            name: String!
            email: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        a = api_gql(
            '''
            query GetA {
                a { child { id name } }
            }
            '''
        )

        b = api_gql(
            '''
            query GetB {
                b { child { id email } }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    assert "class ChildWithIdName(GQLModel):" in generated
    assert "class ChildWithEmailId(GQLModel):" in generated
    assert "class ParentWithChild_ChildWithIdName(GQLModel):" in generated
    assert "class ParentWithChild_ChildWithEmailId(GQLModel):" in generated


async def test_dedup_pydantic_deserialization(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            a: Container!
            b: Container!
        }

        type Container {
            item: Item!
        }

        union Item = Alpha | Beta

        type Alpha {
            id: ID!
            value: String!
        }

        type Beta {
            id: ID!
            score: Int!
        }
    """

    query_source = """
        from sample_app.gql.api import api_gql

        a = api_gql(
            '''
            query GetA {
                a {
                    item {
                        __typename
                        ... on Alpha { id value }
                        ... on Beta { id score }
                    }
                }
            }
            '''
        )

        b = api_gql(
            '''
            query GetB {
                b {
                    item {
                        __typename
                        ... on Alpha { id value }
                        ... on Beta { id score }
                    }
                }
            }
            '''
        )
    """

    def resolve_a(_root, _info):
        return {"item": {"__typename": "Alpha", "id": "a1", "value": "hello"}}

    def resolve_b(_root, _info):
        return {"item": {"__typename": "Beta", "id": "b1", "score": 42}}

    async with test_project.server(
        httpserver,
        schema=schema,
        queries=query_source,
        resolvers={"Query": {"a": resolve_a, "b": resolve_b}},
    ) as (api, queries):
        result_a = await queries.a.execute()
        assert isinstance(result_a.a.item, api.Alpha)
        assert result_a.a.item.value == "hello"

        result_b = await queries.b.execute()
        assert isinstance(result_b.b.item, api.Beta)
        assert result_b.b.item.score == 42


def test_type_alias_references_short_names(test_project: ProjectBuilder):
    schema = """
        union SearchResult = User | Post

        type User {
            id: ID!
            name: String!
        }

        type Post {
            id: ID!
            title: String!
        }

        type Query {
            search: SearchResult
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            query Search {
                search {
                    __typename
                    ... on User { id name }
                    ... on Post { id title }
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    assert "type SearchResultSearch = Annotated[" in generated
    assert "Post" in generated
    assert "User" in generated
    assert "SearchResultSearchPost" not in generated
    assert "SearchResultSearchUser" not in generated


def test_root_models_keep_path_names(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser {
                user { id }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    assert "class GetUserResult(GQLModel):" in generated
    assert "class User(GQLModel):" in generated


def test_idempotent_generation(test_project: ProjectBuilder):
    schema = """
        type Query {
            a: User
            b: User
        }
        type User {
            id: ID!
            name: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        a = api_gql(
            '''
            query GetA { a { id name } }
            '''
        )

        b = api_gql(
            '''
            query GetB { b { id name } }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    assert test_project.generate() is False


def test_stable_across_query_order(test_project: ProjectBuilder):
    schema = """
        type Query {
            a: User
            b: User
        }
        type User {
            id: ID!
            name: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        a = api_gql(
            '''
            query GetA { a { id name } }
            '''
        )

        b = api_gql(
            '''
            query GetB { b { id name } }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    first_gen = (test_project.root / "sample_app/gql/api.py").read_text()

    test_project.write_file(
        test_project.root / "sample_app/queries.py",
        """
        from sample_app.gql.api import api_gql

        b = api_gql(
            '''
            query GetB { b { id name } }
            '''
        )

        a = api_gql(
            '''
            query GetA { a { id name } }
            '''
        )
        """,
    )

    (test_project.root / "sample_app/gql/api.py").unlink()
    assert test_project.generate() is True
    second_gen = (test_project.root / "sample_app/gql/api.py").read_text()

    first_models = [
        line
        for line in first_gen.splitlines()
        if line.startswith("class ") and "GQLModel" in line
    ]
    second_models = [
        line
        for line in second_gen.splitlines()
        if line.startswith("class ") and "GQLModel" in line
    ]
    assert set(first_models) == set(second_models)


def test_repeated_shape_after_collision_reuses_same_model(
    test_project: ProjectBuilder,
):
    schema = """
        type Query {
            a: Parent
            b: Parent
            c: Parent
        }

        type Parent {
            child: Child!
        }

        type Child {
            id: ID!
            name: String!
            email: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        a = api_gql(
            '''
            query GetA { a { child { id name } } }
            '''
        )

        b = api_gql(
            '''
            query GetB { b { child { id email } } }
            '''
        )

        c = api_gql(
            '''
            query GetC { c { child { id name } } }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    child_classes = [
        line
        for line in generated.splitlines()
        if line.startswith("class Child") and "GQLModel" in line
    ]
    assert len(child_classes) == 2

    parent_classes = [
        line
        for line in generated.splitlines()
        if line.startswith("class Parent") and "GQLModel" in line
    ]
    assert len(parent_classes) == 2


def test_field_order_does_not_affect_dedup(test_project: ProjectBuilder):
    schema = """
        type Query {
            a: User
            b: User
        }
        type User {
            id: ID!
            name: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        a = api_gql(
            '''
            query GetA { a { id name } }
            '''
        )

        b = api_gql(
            '''
            query GetB { b { name id } }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    assert generated.count("class User(GQLModel):") == 1
    assert "User_" not in generated


def test_snake_case_fields_use_camel_case_in_model_name(test_project: ProjectBuilder):
    schema = """
        type Query {
            node: Timestamped
        }
        type Timestamped {
            id: ID!
            created_at: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetNode { node { id created_at } }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()

    assert "Timestamped" in generated
    assert "Created_at" not in generated
