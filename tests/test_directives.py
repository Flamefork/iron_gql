import importlib
from collections.abc import Mapping

from pytest_httpserver import HTTPServer

from tests.conftest import ProjectBuilder
from tests.conftest import Resolver
from tests.conftest import build_schema
from tests.conftest import setup_httpserver


async def _execute_get_user(
    test_project: ProjectBuilder,
    httpserver: HTTPServer,
    *,
    schema: str,
    query: str,
    resolver: Resolver,
    variables: Mapping[str, object] | None = None,
):
    query_source = f"""
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            {query}
            '''
        )
    """

    schema_obj = build_schema(schema, {"Query": {"user": resolver}})
    base_url = setup_httpserver(httpserver, schema_obj)
    test_project.prepare(schema=schema, queries=query_source, base_url=base_url)
    test_project.generate()
    test_project.clear_modules()
    api_module = test_project.import_api()
    queries_module = importlib.import_module(f"{test_project.package}.queries")
    try:
        return await queries_module.get_user.execute(**dict(variables or {}))
    finally:
        await api_module.API_CLIENT.close()


async def test_include_skip_directives(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user(id: ID!): User
        }
        type User {
            name: String!
            email: String!
            phone: String!
        }
    """

    def resolve_user(_root, _info, *, id: str):
        return {
            "name": f"Morty {id}",
            "email": f"{id}@example.com",
            "phone": "+34-123",
        }

    query = """
        query GetUser($id: ID!, $withEmail: Boolean!, $skipPhone: Boolean!) {
            user(id: $id) {
                name
                email @include(if: $withEmail)
                phone @skip(if: $skipPhone)
            }
        }
    """

    visible = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"id": "u-1", "with_email": True, "skip_phone": False},
    )
    assert visible.user is not None
    assert visible.user.name == "Morty u-1"
    assert visible.user.email == "u-1@example.com"
    assert visible.user.phone == "+34-123"

    hidden = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"id": "u-1", "with_email": False, "skip_phone": True},
    )
    assert hidden.user is not None
    assert hidden.user.name == "Morty u-1"
    assert hidden.user.email is None
    assert hidden.user.phone is None


async def test_include_on_non_null_field(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String!
        }
    """

    def resolve_user(_root, _info):
        return {"id": "user-1", "name": "Morty"}

    query = """
        query GetUser($withName: Boolean!) {
            user {
                id
                name @include(if: $withName)
            }
        }
    """

    included = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_name": True},
    )
    assert included.user is not None
    assert included.user.id == "user-1"
    assert included.user.name == "Morty"

    omitted = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_name": False},
    )
    assert omitted.user is not None
    assert omitted.user.id == "user-1"
    assert omitted.user.name is None


async def test_skip_on_non_null_field(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String!
        }
    """

    def resolve_user(_root, _info):
        return {"id": "user-1", "name": "Morty"}

    query = """
        query GetUser($skipName: Boolean!) {
            user {
                id
                name @skip(if: $skipName)
            }
        }
    """

    kept = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"skip_name": False},
    )
    assert kept.user is not None
    assert kept.user.id == "user-1"
    assert kept.user.name == "Morty"

    skipped = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"skip_name": True},
    )
    assert skipped.user is not None
    assert skipped.user.id == "user-1"
    assert skipped.user.name is None


async def test_include_on_nullable_field(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String
        }
    """

    def resolve_user(_root, _info):
        return {"id": "user-1", "name": "Morty"}

    query = """
        query GetUser($withName: Boolean!) {
            user {
                id
                name @include(if: $withName)
            }
        }
    """

    included = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_name": True},
    )
    assert included.user is not None
    assert included.user.name == "Morty"

    omitted = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_name": False},
    )
    assert omitted.user is not None
    assert omitted.user.name is None


async def test_include_on_inline_fragment(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
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

    def resolve_user(_root, _info):
        return {"id": "user-1", "name": "Morty", "email": "morty@example.com"}

    query = """
        query GetUser($withDetails: Boolean!) {
            user {
                id
                ... @include(if: $withDetails) {
                    name
                    email
                }
            }
        }
    """

    included = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_details": True},
    )
    assert included.user is not None
    assert included.user.id == "user-1"
    assert included.user.name == "Morty"
    assert included.user.email == "morty@example.com"

    omitted = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_details": False},
    )
    assert omitted.user is not None
    assert omitted.user.id == "user-1"
    assert omitted.user.name is None
    assert omitted.user.email is None


async def test_field_both_conditional_and_unconditional(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String!
        }
    """

    def resolve_user(_root, _info):
        return {"id": "user-1", "name": "Morty"}

    result = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query="""
            query GetUser($withDetails: Boolean!) {
                user {
                    id
                    name
                    ... @include(if: $withDetails) {
                        name
                    }
                }
            }
        """,
        resolver=resolve_user,
        variables={"with_details": False},
    )
    assert result.user is not None
    assert result.user.id == "user-1"
    assert result.user.name == "Morty"


async def test_skip_with_literal_false(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String!
        }
    """

    def resolve_user(_root, _info):
        return {"id": "user-1", "name": "Morty"}

    result = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query="""
            query GetUser {
                user {
                    id
                    name @skip(if: false)
                }
            }
        """,
        resolver=resolve_user,
    )
    assert result.user is not None
    assert result.user.id == "user-1"
    assert result.user.name == "Morty"


async def test_include_and_skip_on_same_field(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String!
        }
    """

    def resolve_user(_root, _info):
        return {"id": "user-1", "name": "Morty"}

    query = """
        query GetUser($show: Boolean!, $hide: Boolean!) {
            user {
                id
                name @include(if: $show) @skip(if: $hide)
            }
        }
    """

    visible = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"show": True, "hide": False},
    )
    assert visible.user is not None
    assert visible.user.name == "Morty"

    omitted = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"show": True, "hide": True},
    )
    assert omitted.user is not None
    assert omitted.user.name is None


async def test_include_on_camel_case_field(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            firstName: String!
        }
    """

    def resolve_user(_root, _info):
        return {"id": "user-1", "firstName": "Morty"}

    query = """
        query GetUser($withName: Boolean!) {
            user {
                id
                firstName @include(if: $withName)
            }
        }
    """

    included = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_name": True},
    )
    assert included.user is not None
    assert included.user.first_name == "Morty"

    omitted = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_name": False},
    )
    assert omitted.user is not None
    assert omitted.user.first_name is None


async def test_include_on_non_null_list_of_nullable(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            tags: [String]!
        }
    """

    def resolve_user(_root, _info):
        return {"id": "user-1", "tags": ["vip", None]}

    query = """
        query GetUser($withTags: Boolean!) {
            user {
                id
                tags @include(if: $withTags)
            }
        }
    """

    included = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_tags": True},
    )
    assert included.user is not None
    assert included.user.tags == ["vip", None]

    omitted = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_tags": False},
    )
    assert omitted.user is not None
    assert omitted.user.tags is None


async def test_include_with_literal_true(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
    schema = """
        type Query {
            user: User
        }
        type User {
            id: ID!
            name: String!
        }
    """

    def resolve_user(_root, _info):
        return {"id": "user-1", "name": "Morty"}

    result = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query="""
            query GetUser {
                user {
                    id
                    name @include(if: true)
                }
            }
        """,
        resolver=resolve_user,
    )
    assert result.user is not None
    assert result.user.id == "user-1"
    assert result.user.name == "Morty"


async def test_include_on_nested_object_field(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
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

    def resolve_user(_root, _info):
        return {
            "id": "user-1",
            "address": {"city": "Madrid", "zip": "28001"},
        }

    query = """
        query GetUser($withAddress: Boolean!) {
            user {
                id
                address @include(if: $withAddress) {
                    city
                    zip
                }
            }
        }
    """

    included = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_address": True},
    )
    assert included.user is not None
    assert included.user.address is not None
    assert included.user.address.city == "Madrid"
    assert included.user.address.zip == "28001"

    omitted = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"with_address": False},
    )
    assert omitted.user is not None
    assert omitted.user.address is None


async def test_shared_variable_in_include_and_skip(
    test_project: ProjectBuilder, httpserver: HTTPServer
):
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

    def resolve_user(_root, _info):
        return {
            "id": "user-1",
            "email": "morty@example.com",
            "phone": "+34-123",
        }

    query = """
        query GetUser($flag: Boolean!) {
            user {
                id
                email @include(if: $flag)
                phone @skip(if: $flag)
            }
        }
    """

    enabled = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"flag": True},
    )
    assert enabled.user is not None
    assert enabled.user.email == "morty@example.com"
    assert enabled.user.phone is None

    disabled = await _execute_get_user(
        test_project,
        httpserver,
        schema=schema,
        query=query,
        resolver=resolve_user,
        variables={"flag": False},
    )
    assert disabled.user is not None
    assert disabled.user.email is None
    assert disabled.user.phone == "+34-123"
