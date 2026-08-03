import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from tests.conftest import generated_package
from tests.conftest import gql_server

generated_package(
    "directives_include_skip",
    schema="""
    type Query {
        user(id: ID!): User
    }
    type User {
        name: String!
        email: String!
        phone: String!
    }
    """,
    queries='''
    from tests.generated.directives_include_skip.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($id: ID!, $withEmail: Boolean!, $skipPhone: Boolean!) {
            user(id: $id) {
                name
                email @include(if: $withEmail)
                phone @skip(if: $skipPhone)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_include_non_null",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
    }
    """,
    queries='''
    from tests.generated.directives_include_non_null.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($withName: Boolean!) {
            user {
                id
                name @include(if: $withName)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_skip_non_null",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
    }
    """,
    queries='''
    from tests.generated.directives_skip_non_null.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($skipName: Boolean!) {
            user {
                id
                name @skip(if: $skipName)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_include_nullable",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String
    }
    """,
    queries='''
    from tests.generated.directives_include_nullable.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($withName: Boolean!) {
            user {
                id
                name @include(if: $withName)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_include_inline_fragment",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
        email: String!
    }
    """,
    queries='''
    from tests.generated.directives_include_inline_fragment.gql.api import api_gql

    get_user = api_gql(
        """
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
    )
    ''',
)

generated_package(
    "directives_conditional_and_unconditional",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
    }
    """,
    queries='''
    from tests.generated.directives_conditional_and_unconditional.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($withDetails: Boolean!) {
            user {
                id
                name
                ... @include(if: $withDetails) {
                    name
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_skip_literal_false",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
    }
    """,
    queries='''
    from tests.generated.directives_skip_literal_false.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser {
            user {
                id
                name @skip(if: false)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_include_skip_same_field",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
    }
    """,
    queries='''
    from tests.generated.directives_include_skip_same_field.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($show: Boolean!, $hide: Boolean!) {
            user {
                id
                name @include(if: $show) @skip(if: $hide)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_include_camel_case",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        firstName: String!
    }
    """,
    queries='''
    from tests.generated.directives_include_camel_case.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($withName: Boolean!) {
            user {
                id
                firstName @include(if: $withName)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_include_list",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        tags: [String]!
    }
    """,
    queries='''
    from tests.generated.directives_include_list.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($withTags: Boolean!) {
            user {
                id
                tags @include(if: $withTags)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_include_literal_true",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
    }
    """,
    queries='''
    from tests.generated.directives_include_literal_true.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser {
            user {
                id
                name @include(if: true)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_include_nested_object",
    schema="""
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
    """,
    queries='''
    from tests.generated.directives_include_nested_object.gql.api import api_gql

    get_user = api_gql(
        """
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
    )
    ''',
)

generated_package(
    "directives_shared_variable",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        email: String!
        phone: String!
    }
    """,
    queries='''
    from tests.generated.directives_shared_variable.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($flag: Boolean!) {
            user {
                id
                email @include(if: $flag)
                phone @skip(if: $flag)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_inline_literal_false",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
    }
    """,
    queries='''
    from tests.generated.directives_inline_literal_false.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser {
            user {
                id
                ... @include(if: false) { name }
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_contradictory_pair",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
    }
    """,
    queries='''
    from tests.generated.directives_contradictory_pair.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($flag: Boolean!) {
            user {
                id
                name @include(if: $flag) @skip(if: $flag)
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_mixed_polarity_variable",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
        email: String!
    }
    """,
    queries='''
    from tests.generated.directives_mixed_polarity_variable.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($a: Boolean!, $b: Boolean!) {
            user {
                id
                ... @include(if: $b) { name }
                ... @include(if: $a) @skip(if: $b) { email }
            }
        }
        """
    )
    ''',
)

generated_package(
    "directives_complementary_conjunctions",
    schema="""
    type Query {
        user: User
    }
    type User {
        id: ID!
        name: String!
    }
    """,
    queries='''
    from tests.generated.directives_complementary_conjunctions.gql.api import api_gql

    get_user = api_gql(
        """
        query GetUser($a: Boolean!, $b: Boolean!) {
            user {
                id
                ... @include(if: $a) { ... @include(if: $b) { name } }
                ... @skip(if: $a) { ... @skip(if: $b) { name } }
            }
        }
        """
    )
    ''',
)

from tests.generated.directives_complementary_conjunctions import (
    queries as complementary_conjunctions_queries,
)
from tests.generated.directives_conditional_and_unconditional import (
    queries as conditional_and_unconditional_queries,
)
from tests.generated.directives_contradictory_pair import (
    queries as contradictory_pair_queries,
)
from tests.generated.directives_include_camel_case import (
    queries as include_camel_case_queries,
)
from tests.generated.directives_include_inline_fragment import (
    queries as include_inline_fragment_queries,
)
from tests.generated.directives_include_list import queries as include_list_queries
from tests.generated.directives_include_literal_true import (
    queries as include_literal_true_queries,
)
from tests.generated.directives_include_nested_object import (
    queries as include_nested_object_queries,
)
from tests.generated.directives_include_non_null import (
    queries as include_non_null_queries,
)
from tests.generated.directives_include_nullable import (
    queries as include_nullable_queries,
)
from tests.generated.directives_include_skip import queries as include_skip_queries
from tests.generated.directives_include_skip_same_field import (
    queries as include_skip_same_field_queries,
)
from tests.generated.directives_inline_literal_false import (
    queries as inline_literal_false_queries,
)
from tests.generated.directives_mixed_polarity_variable import (
    queries as mixed_polarity_variable_queries,
)
from tests.generated.directives_shared_variable import (
    queries as shared_variable_queries,
)
from tests.generated.directives_skip_literal_false import (
    queries as skip_literal_false_queries,
)
from tests.generated.directives_skip_non_null import queries as skip_non_null_queries


async def test_include_skip_directives(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        return {
            "name": f"Bob {id}",
            "email": f"{id}@example.com",
            "phone": "+34-123",
        }

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_include_skip",
        {"Query": {"user": resolve_user}},
    ):
        visible = await include_skip_queries.get_user.execute(
            id="u-1", with_email=True, skip_phone=False
        )
        assert visible.user is not None
        assert visible.user.name == "Bob u-1"
        assert visible.user.email == "u-1@example.com"
        assert visible.user.phone == "+34-123"

        hidden = await include_skip_queries.get_user.execute(
            id="u-1", with_email=False, skip_phone=True
        )
        assert hidden.user is not None
        assert hidden.user.name == "Bob u-1"
        assert hidden.user.email is None
        assert hidden.user.phone is None


async def test_include_on_non_null_field(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_include_non_null",
        {"Query": {"user": resolve_user}},
    ):
        included = await include_non_null_queries.get_user.execute(with_name=True)
        assert included.user is not None
        assert included.user.id == "user-1"
        assert included.user.name == "Bob"

        omitted = await include_non_null_queries.get_user.execute(with_name=False)
        assert omitted.user is not None
        assert omitted.user.id == "user-1"
        assert omitted.user.name is None


async def test_skip_on_non_null_field(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_skip_non_null",
        {"Query": {"user": resolve_user}},
    ):
        kept = await skip_non_null_queries.get_user.execute(skip_name=False)
        assert kept.user is not None
        assert kept.user.id == "user-1"
        assert kept.user.name == "Bob"

        skipped = await skip_non_null_queries.get_user.execute(skip_name=True)
        assert skipped.user is not None
        assert skipped.user.id == "user-1"
        assert skipped.user.name is None


async def test_include_on_nullable_field(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_include_nullable",
        {"Query": {"user": resolve_user}},
    ):
        included = await include_nullable_queries.get_user.execute(with_name=True)
        assert included.user is not None
        assert included.user.name == "Bob"

        omitted = await include_nullable_queries.get_user.execute(with_name=False)
        assert omitted.user is not None
        assert omitted.user.name is None


async def test_include_on_inline_fragment(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob", "email": "bob@example.com"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_include_inline_fragment",
        {"Query": {"user": resolve_user}},
    ):
        included = await include_inline_fragment_queries.get_user.execute(
            with_details=True
        )
        assert included.user is not None
        assert included.user.id == "user-1"
        assert included.user.name == "Bob"
        assert included.user.email == "bob@example.com"

        omitted = await include_inline_fragment_queries.get_user.execute(
            with_details=False
        )
        assert omitted.user is not None
        assert omitted.user.id == "user-1"
        assert omitted.user.name is None
        assert omitted.user.email is None


async def test_field_both_conditional_and_unconditional(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_conditional_and_unconditional",
        {"Query": {"user": resolve_user}},
    ):
        result = await conditional_and_unconditional_queries.get_user.execute(
            with_details=False
        )
        assert result.user is not None
        assert result.user.id == "user-1"
        assert result.user.name == "Bob"


async def test_skip_with_literal_false(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_skip_literal_false",
        {"Query": {"user": resolve_user}},
    ):
        result = await skip_literal_false_queries.get_user.execute()
        assert result.user is not None
        assert result.user.id == "user-1"
        assert result.user.name == "Bob"


async def test_include_and_skip_on_same_field(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_include_skip_same_field",
        {"Query": {"user": resolve_user}},
    ):
        visible = await include_skip_same_field_queries.get_user.execute(
            show=True, hide=False
        )
        assert visible.user is not None
        assert visible.user.name == "Bob"

        omitted = await include_skip_same_field_queries.get_user.execute(
            show=True, hide=True
        )
        assert omitted.user is not None
        assert omitted.user.name is None


async def test_include_on_camel_case_field(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "firstName": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_include_camel_case",
        {"Query": {"user": resolve_user}},
    ):
        included = await include_camel_case_queries.get_user.execute(with_name=True)
        assert included.user is not None
        assert included.user.first_name == "Bob"

        omitted = await include_camel_case_queries.get_user.execute(with_name=False)
        assert omitted.user is not None
        assert omitted.user.first_name is None


async def test_include_on_non_null_list_of_nullable(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(
        _root: None, _info: GraphQLResolveInfo
    ) -> dict[str, str | list[str | None]]:
        return {"id": "user-1", "tags": ["vip", None]}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_include_list",
        {"Query": {"user": resolve_user}},
    ):
        included = await include_list_queries.get_user.execute(with_tags=True)
        assert included.user is not None
        assert included.user.tags == ["vip", None]

        omitted = await include_list_queries.get_user.execute(with_tags=False)
        assert omitted.user is not None
        assert omitted.user.tags is None


async def test_include_with_literal_true(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_include_literal_true",
        {"Query": {"user": resolve_user}},
    ):
        result = await include_literal_true_queries.get_user.execute()
        assert result.user is not None
        assert result.user.id == "user-1"
        assert result.user.name == "Bob"


async def test_include_on_nested_object_field(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(
        _root: None, _info: GraphQLResolveInfo
    ) -> dict[str, str | dict[str, str]]:
        return {
            "id": "user-1",
            "address": {"city": "Madrid", "zip": "28001"},
        }

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_include_nested_object",
        {"Query": {"user": resolve_user}},
    ):
        included = await include_nested_object_queries.get_user.execute(
            with_address=True
        )
        assert included.user is not None
        assert included.user.address is not None
        assert included.user.address.city == "Madrid"
        assert included.user.address.zip == "28001"

        omitted = await include_nested_object_queries.get_user.execute(
            with_address=False
        )
        assert omitted.user is not None
        assert omitted.user.address is None


async def test_inline_fragment_with_literal_false_is_statically_excluded(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    # `... @include(if: false)` cuts the whole subtree at generation time: the
    # model never grows a `name` field, and the query still executes.
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_inline_literal_false",
        {"Query": {"user": resolve_user}},
    ):
        result = await inline_literal_false_queries.get_user.execute()
        assert result.user is not None
        assert result.user.id == "user-1"
        assert "name" not in type(result.user).model_fields


async def test_contradictory_directive_pair_is_statically_excluded(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    # `@include(if: $flag) @skip(if: $flag)` can never both hold, whatever
    # $flag is: the field is statically excluded — no model field, and the
    # query executes at either value.
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_contradictory_pair",
        {"Query": {"user": resolve_user}},
    ):
        result = await contradictory_pair_queries.get_user.execute(flag=True)
        assert result.user is not None
        assert result.user.id == "user-1"
        assert "name" not in type(result.user).model_fields


async def test_field_selected_only_between_variable_extremes(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    # $b guards `name` via @include and `email` via @skip, so no assignment
    # shows both fields at once: email exists only at $a=true, $b=false. The
    # generated model must still admit that state — dropping the field would
    # make `extra="forbid"` reject a valid response.
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {
            "id": "user-1",
            "name": "Bob",
            "email": "bob@example.com",
        }

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_mixed_polarity_variable",
        {"Query": {"user": resolve_user}},
    ):
        with_email = await mixed_polarity_variable_queries.get_user.execute(
            a=True, b=False
        )
        assert with_email.user is not None
        assert with_email.user.email == "bob@example.com"
        assert with_email.user.name is None

        with_name = await mixed_polarity_variable_queries.get_user.execute(
            a=False, b=True
        )
        assert with_name.user is not None
        assert with_name.user.name == "Bob"
        assert with_name.user.email is None


async def test_key_absent_between_complementary_conjunctions(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    # `name` is selected at $a=$b=true and at $a=$b=false but at neither mixed
    # assignment, so the field must be optional even though both all-true and
    # all-false states show it.
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {"id": "user-1", "name": "Bob"}

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_complementary_conjunctions",
        {"Query": {"user": resolve_user}},
    ):
        both = await complementary_conjunctions_queries.get_user.execute(a=True, b=True)
        assert both.user is not None
        assert both.user.name == "Bob"

        mixed = await complementary_conjunctions_queries.get_user.execute(
            a=True, b=False
        )
        assert mixed.user is not None
        assert mixed.user.name is None


async def test_shared_variable_in_include_and_skip(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
):
    def resolve_user(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        return {
            "id": "user-1",
            "email": "bob@example.com",
            "phone": "+34-123",
        }

    async with gql_server(
        httpserver,
        monkeypatch,
        "directives_shared_variable",
        {"Query": {"user": resolve_user}},
    ):
        enabled = await shared_variable_queries.get_user.execute(flag=True)
        assert enabled.user is not None
        assert enabled.user.email == "bob@example.com"
        assert enabled.user.phone is None

        disabled = await shared_variable_queries.get_user.execute(flag=False)
        assert disabled.user is not None
        assert disabled.user.email is None
        assert disabled.user.phone == "+34-123"
