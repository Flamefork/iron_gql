"""`@skip`/`@include`, as one table over one package.

Every row here used to be its own committed package with its own three-field
schema -- seventeen of them, differing in which field was conditional. The
axes are the directive (`include`/`skip`), what the condition is (a variable,
a literal, two variables in combination), and what it guards (a field, an
inline fragment, a nested object, a list, a camel-cased name). Crossing them
on one schema is what makes a missing row visible; seventeen near-identical
schemas only made each row look covered.
"""

from collections.abc import AsyncIterator

import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from tests.conftest import Resolvers
from tests.conftest import generated_package
from tests.conftest import gql_server

PACKAGE = "directives"

# One schema, wide enough for every row. `nullableName` is its own field
# rather than a nullability the schema flips per package: a row that asks what
# a directive does to an already-nullable field is a different cell, not a
# different schema.
SCHEMA = """
type Query {
    user(id: ID): User
}

type User {
    id: ID!
    name: String!
    nullableName: String
    email: String!
    phone: String!
    firstName: String!
    tags: [String]!
    address: Address!
}

type Address {
    city: String!
    zip: String!
}
"""

generated_package(
    PACKAGE,
    schema=SCHEMA,
    queries='''
    from tests.generated.directives.gql.api import api_gql

    include_skip = api_gql(
        """
        query IncludeSkip($id: ID!, $withEmail: Boolean!, $skipPhone: Boolean!) {
            user(id: $id) {
                name
                email @include(if: $withEmail)
                phone @skip(if: $skipPhone)
            }
        }
        """
    )

    include_non_null = api_gql(
        """
        query IncludeNonNull($withName: Boolean!) {
            user {
                id
                name @include(if: $withName)
            }
        }
        """
    )

    skip_non_null = api_gql(
        """
        query SkipNonNull($skipName: Boolean!) {
            user {
                id
                name @skip(if: $skipName)
            }
        }
        """
    )

    include_nullable = api_gql(
        """
        query IncludeNullable($withName: Boolean!) {
            user {
                id
                nullableName @include(if: $withName)
            }
        }
        """
    )

    include_inline_fragment = api_gql(
        """
        query IncludeInlineFragment($withDetails: Boolean!) {
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

    conditional_and_unconditional = api_gql(
        """
        query ConditionalAndUnconditional($withDetails: Boolean!) {
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

    skip_literal_false = api_gql(
        """
        query SkipLiteralFalse {
            user {
                id
                name @skip(if: false)
            }
        }
        """
    )

    include_skip_same_field = api_gql(
        """
        query IncludeSkipSameField($show: Boolean!, $hide: Boolean!) {
            user {
                id
                name @include(if: $show) @skip(if: $hide)
            }
        }
        """
    )

    include_camel_case = api_gql(
        """
        query IncludeCamelCase($withName: Boolean!) {
            user {
                id
                firstName @include(if: $withName)
            }
        }
        """
    )

    include_list = api_gql(
        """
        query IncludeList($withTags: Boolean!) {
            user {
                id
                tags @include(if: $withTags)
            }
        }
        """
    )

    include_literal_true = api_gql(
        """
        query IncludeLiteralTrue {
            user {
                id
                name @include(if: true)
            }
        }
        """
    )

    include_nested_object = api_gql(
        """
        query IncludeNestedObject($withAddress: Boolean!) {
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

    shared_variable = api_gql(
        """
        query SharedVariable($flag: Boolean!) {
            user {
                id
                email @include(if: $flag)
                phone @skip(if: $flag)
            }
        }
        """
    )

    inline_literal_false = api_gql(
        """
        query InlineLiteralFalse {
            user {
                id
                ... @include(if: false) { name }
            }
        }
        """
    )

    contradictory_pair = api_gql(
        """
        query ContradictoryPair($flag: Boolean!) {
            user {
                id
                name @include(if: $flag) @skip(if: $flag)
            }
        }
        """
    )

    mixed_polarity_variable = api_gql(
        """
        query MixedPolarityVariable($a: Boolean!, $b: Boolean!) {
            user {
                id
                ... @include(if: $b) { name }
                ... @include(if: $a) @skip(if: $b) { email }
            }
        }
        """
    )

    complementary_conjunctions = api_gql(
        """
        query ComplementaryConjunctions($a: Boolean!, $b: Boolean!) {
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

from tests.generated.directives import queries

USER: dict[str, object] = {
    "id": "user-1",
    "name": "Bob",
    "nullableName": "Bob",
    "email": "bob@example.com",
    "phone": "+34-123",
    "firstName": "Bob",
    "tags": ["vip", None],
    "address": {"city": "Madrid", "zip": "28001"},
}


def _resolve_user(
    _root: None, _info: GraphQLResolveInfo, **arguments: object
) -> dict[str, object]:
    # One resolver for every row: which fields reach the response is the
    # directives' business, not the server's, so it always offers all of them
    # and each row observes what came back. `**arguments` because only one row
    # passes `id`.
    identifier = arguments.get("id", USER["id"])
    return {**USER, "id": identifier}


RESOLVERS: Resolvers = {"Query": {"user": _resolve_user}}


@pytest.fixture
async def server(httpserver: HTTPServer) -> AsyncIterator[None]:
    async with gql_server(httpserver, PACKAGE, RESOLVERS):
        yield


@pytest.mark.usefixtures("server")
async def test_include_skip_directives():
    visible = await queries.include_skip.execute(
        id="u-1", with_email=True, skip_phone=False
    )
    assert visible.user is not None
    assert visible.user.name == "Bob"
    assert visible.user.email == "bob@example.com"
    assert visible.user.phone == "+34-123"

    hidden = await queries.include_skip.execute(
        id="u-1", with_email=False, skip_phone=True
    )
    assert hidden.user is not None
    assert hidden.user.name == "Bob"
    assert hidden.user.email is None
    assert hidden.user.phone is None


@pytest.mark.usefixtures("server")
async def test_include_on_non_null_field():
    included = await queries.include_non_null.execute(with_name=True)
    assert included.user is not None
    assert included.user.id == "user-1"
    assert included.user.name == "Bob"

    omitted = await queries.include_non_null.execute(with_name=False)
    assert omitted.user is not None
    assert omitted.user.id == "user-1"
    assert omitted.user.name is None


@pytest.mark.usefixtures("server")
async def test_skip_on_non_null_field():
    kept = await queries.skip_non_null.execute(skip_name=False)
    assert kept.user is not None
    assert kept.user.name == "Bob"

    skipped = await queries.skip_non_null.execute(skip_name=True)
    assert skipped.user is not None
    assert skipped.user.name is None


@pytest.mark.usefixtures("server")
async def test_include_on_nullable_field():
    included = await queries.include_nullable.execute(with_name=True)
    assert included.user is not None
    assert included.user.nullable_name == "Bob"

    omitted = await queries.include_nullable.execute(with_name=False)
    assert omitted.user is not None
    assert omitted.user.nullable_name is None


@pytest.mark.usefixtures("server")
async def test_include_on_inline_fragment():
    included = await queries.include_inline_fragment.execute(with_details=True)
    assert included.user is not None
    assert included.user.name == "Bob"
    assert included.user.email == "bob@example.com"

    omitted = await queries.include_inline_fragment.execute(with_details=False)
    assert omitted.user is not None
    assert omitted.user.id == "user-1"
    assert omitted.user.name is None
    assert omitted.user.email is None


@pytest.mark.usefixtures("server")
async def test_field_both_conditional_and_unconditional():
    result = await queries.conditional_and_unconditional.execute(with_details=False)
    assert result.user is not None
    assert result.user.id == "user-1"
    assert result.user.name == "Bob"


@pytest.mark.usefixtures("server")
async def test_skip_with_literal_false():
    result = await queries.skip_literal_false.execute()
    assert result.user is not None
    assert result.user.id == "user-1"
    assert result.user.name == "Bob"


@pytest.mark.usefixtures("server")
async def test_include_and_skip_on_same_field():
    visible = await queries.include_skip_same_field.execute(show=True, hide=False)
    assert visible.user is not None
    assert visible.user.name == "Bob"

    omitted = await queries.include_skip_same_field.execute(show=True, hide=True)
    assert omitted.user is not None
    assert omitted.user.name is None


@pytest.mark.usefixtures("server")
async def test_include_on_camel_case_field():
    included = await queries.include_camel_case.execute(with_name=True)
    assert included.user is not None
    assert included.user.first_name == "Bob"

    omitted = await queries.include_camel_case.execute(with_name=False)
    assert omitted.user is not None
    assert omitted.user.first_name is None


@pytest.mark.usefixtures("server")
async def test_include_on_non_null_list_of_nullable():
    included = await queries.include_list.execute(with_tags=True)
    assert included.user is not None
    assert included.user.tags == ["vip", None]

    omitted = await queries.include_list.execute(with_tags=False)
    assert omitted.user is not None
    assert omitted.user.tags is None


@pytest.mark.usefixtures("server")
async def test_include_with_literal_true():
    result = await queries.include_literal_true.execute()
    assert result.user is not None
    assert result.user.id == "user-1"
    assert result.user.name == "Bob"


@pytest.mark.usefixtures("server")
async def test_include_on_nested_object_field():
    included = await queries.include_nested_object.execute(with_address=True)
    assert included.user is not None
    assert included.user.address is not None
    assert included.user.address.city == "Madrid"
    assert included.user.address.zip == "28001"

    omitted = await queries.include_nested_object.execute(with_address=False)
    assert omitted.user is not None
    assert omitted.user.address is None


@pytest.mark.usefixtures("server")
async def test_inline_fragment_with_literal_false_is_statically_excluded():
    # `... @include(if: false)` cuts the whole subtree at generation time: the
    # model never grows a `name` field, and the query still executes.
    result = await queries.inline_literal_false.execute()
    assert result.user is not None
    assert result.user.id == "user-1"
    assert "name" not in type(result.user).model_fields


@pytest.mark.usefixtures("server")
async def test_contradictory_directive_pair_is_statically_excluded():
    # `@include(if: $flag) @skip(if: $flag)` can never both hold, whatever
    # $flag is: the field is statically excluded — no model field, and the
    # query executes at either value.
    result = await queries.contradictory_pair.execute(flag=True)
    assert result.user is not None
    assert result.user.id == "user-1"
    assert "name" not in type(result.user).model_fields


@pytest.mark.usefixtures("server")
async def test_field_selected_only_between_variable_extremes():
    # $b guards `name` via @include and `email` via @skip, so no assignment
    # shows both fields at once: email exists only at $a=true, $b=false. The
    # generated model must still admit that state — dropping the field would
    # make `extra="forbid"` reject a valid response.
    with_email = await queries.mixed_polarity_variable.execute(a=True, b=False)
    assert with_email.user is not None
    assert with_email.user.email == "bob@example.com"
    assert with_email.user.name is None

    with_name = await queries.mixed_polarity_variable.execute(a=False, b=True)
    assert with_name.user is not None
    assert with_name.user.name == "Bob"
    assert with_name.user.email is None


@pytest.mark.usefixtures("server")
async def test_key_absent_between_complementary_conjunctions():
    # `name` is selected at $a=$b=true and at $a=$b=false but at neither mixed
    # assignment, so the field must be optional even though both all-true and
    # all-false states show it.
    both = await queries.complementary_conjunctions.execute(a=True, b=True)
    assert both.user is not None
    assert both.user.name == "Bob"

    mixed = await queries.complementary_conjunctions.execute(a=True, b=False)
    assert mixed.user is not None
    assert mixed.user.name is None


@pytest.mark.usefixtures("server")
async def test_shared_variable_in_include_and_skip():
    enabled = await queries.shared_variable.execute(flag=True)
    assert enabled.user is not None
    assert enabled.user.email == "bob@example.com"
    assert enabled.user.phone is None

    disabled = await queries.shared_variable.execute(flag=False)
    assert disabled.user is not None
    assert disabled.user.email is None
    assert disabled.user.phone == "+34-123"
