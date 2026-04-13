from tests.conftest import ProjectBuilder


def test_include_skip_directives(test_project: ProjectBuilder):
    schema = """
        type Query {
            user(id: ID!): User
        }
        type User {
            id: ID!
            name: String!
            email: String!
            phone: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            query GetUser($id: ID!, $withEmail: Boolean!, $skipPhone: Boolean!) {
                user(id: $id) {
                    name
                    email @include(if: $withEmail)
                    phone @skip(if: $skipPhone)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "name: str\n" in generated
    assert "email: str | None = None" in generated
    assert "phone: str | None = None" in generated


def test_include_on_non_null_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        q = api_gql(
            '''
            query GetUser($withName: Boolean!) {
                user {
                    id
                    name @include(if: $withName)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str | None = None" in generated


def test_skip_on_non_null_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        q = api_gql(
            '''
            query GetUser($skipName: Boolean!) {
                user {
                    id
                    name @skip(if: $skipName)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str | None = None" in generated


def test_include_on_nullable_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($withName: Boolean!) {
                user {
                    id
                    name @include(if: $withName)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str | None = None" in generated


def test_include_on_inline_fragment(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        q = api_gql(
            '''
            query GetUser($withDetails: Boolean!) {
                user {
                    id
                    ... @include(if: $withDetails) {
                        name
                        email
                    }
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str | None = None" in generated
    assert "email: str | None = None" in generated


def test_field_both_conditional_and_unconditional(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        q = api_gql(
            '''
            query GetUser($withDetails: Boolean!) {
                user {
                    id
                    name
                    ... @include(if: $withDetails) {
                        name
                    }
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str\n" in generated


def test_skip_with_literal_false(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        q = api_gql(
            '''
            query GetUser {
                user {
                    id
                    name @skip(if: false)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str\n" in generated


def test_include_and_skip_on_same_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        q = api_gql(
            '''
            query GetUser($show: Boolean!, $hide: Boolean!) {
                user {
                    id
                    name @include(if: $show) @skip(if: $hide)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "name: str | None = None" in generated


def test_include_on_camel_case_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            firstName: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($withName: Boolean!) {
                user {
                    id
                    firstName @include(if: $withName)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "first_name: str | None = None" in generated


def test_include_on_non_null_list_of_nullable(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            tags: [String]!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($withTags: Boolean!) {
                user {
                    id
                    tags @include(if: $withTags)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "tags: list[str | None] | None = None" in generated


def test_include_with_literal_true(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        q = api_gql(
            '''
            query GetUser {
                user {
                    id
                    name @include(if: true)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str\n" in generated


def test_include_on_nested_object_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            address: Address!
        }
        type Address {
            city: String!
            zip: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($withAddress: Boolean!) {
                user {
                    id
                    address @include(if: $withAddress) {
                        city
                        zip
                    }
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "address: Address | None = None" in generated
    assert "class Address(GQLModel):" in generated
    assert "city: str\n" in generated
    assert "zip: str\n" in generated


def test_shared_variable_in_include_and_skip(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            email: String!
            phone: String!
        }
    """
    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query GetUser($flag: Boolean!) {
                user {
                    id
                    email @include(if: $flag)
                    phone @skip(if: $flag)
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "email: str | None = None" in generated
    assert "phone: str | None = None" in generated


def test_include_skip_inside_named_fragment(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
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

        q = api_gql(
            '''
            query GetUser($withEmail: Boolean!) {
                user {
                    id
                    ...UserDetails
                }
            }

            fragment UserDetails on User {
                name
                email @include(if: $withEmail)
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "id: builtins.str\n" in generated
    assert "name: str\n" in generated
    assert "email: str | None = None" in generated
