from iron_gql import runtime
from tests.conftest import generated_package

generated_package(
    "oneof",
    schema="""
    type Query {
        ping: Boolean
    }

    type Mutation {
        search(criteria: SearchCriteria!): Boolean
        update(input: UpdateAction!): Boolean
        doSearch(input: WrapperInput!): Boolean
        act(input: SingleChoice!): Boolean
        filter(by: FilterBy!): Boolean
        searchBy(by: SearchBy!): Boolean
        actNested(input: OuterChoice!): Boolean
    }

    input SearchCriteria @oneOf {
        name: String
        email: String
        userId: ID
    }

    input AddressInput {
        street: String!
        city: String!
    }

    input UpdateAction @oneOf {
        setName: String
        setAddress: AddressInput
    }

    input WrapperInput {
        criteria: SearchCriteria!
        limit: Int
    }

    input SingleChoice @oneOf {
        value: String
    }

    enum Status {
        ACTIVE
        INACTIVE
    }

    input FilterBy @oneOf {
        status: Status
        name: String
    }

    input SearchBy @oneOf {
        name: String
        tags: [String!]
    }

    input InnerChoice @oneOf {
        x: String
        y: Int
    }

    input OuterChoice @oneOf {
        inner: InnerChoice
        direct: String
    }
    """,
    queries='''
    from tests.generated.oneof.gql.api import api_gql

    search = api_gql(
        """
        mutation Search($criteria: SearchCriteria!) {
            search(criteria: $criteria)
        }
        """
    )

    update = api_gql(
        """
        mutation Update($input: UpdateAction!) {
            update(input: $input)
        }
        """
    )

    do_search = api_gql(
        """
        mutation DoSearch($input: WrapperInput!) {
            doSearch(input: $input)
        }
        """
    )

    act = api_gql(
        """
        mutation Act($input: SingleChoice!) {
            act(input: $input)
        }
        """
    )

    do_filter = api_gql(
        """
        mutation Filter($by: FilterBy!) {
            filter(by: $by)
        }
        """
    )

    list_search = api_gql(
        """
        mutation ListSearch($by: SearchBy!) {
            searchBy(by: $by)
        }
        """
    )

    act_nested = api_gql(
        """
        mutation ActNested($input: OuterChoice!) {
            actNested(input: $input)
        }
        """
    )
    ''',
)

from tests.generated.oneof.gql.api import AddressInput
from tests.generated.oneof.gql.api import FilterByName
from tests.generated.oneof.gql.api import FilterByStatus
from tests.generated.oneof.gql.api import InnerChoiceX
from tests.generated.oneof.gql.api import OuterChoiceDirect
from tests.generated.oneof.gql.api import OuterChoiceInner
from tests.generated.oneof.gql.api import SearchByName
from tests.generated.oneof.gql.api import SearchByTags
from tests.generated.oneof.gql.api import SearchCriteriaEmail
from tests.generated.oneof.gql.api import SearchCriteriaName
from tests.generated.oneof.gql.api import SearchCriteriaUserId
from tests.generated.oneof.gql.api import SingleChoiceValue
from tests.generated.oneof.gql.api import UpdateActionSetAddress
from tests.generated.oneof.gql.api import WrapperInput


def test_one_of_basic_scalar_fields():
    by_name = SearchCriteriaName(name="John")
    assert runtime.serialize_variables({"x": by_name})[0]["x"] == {"name": "John"}

    by_email = SearchCriteriaEmail(email="j@example.com")
    assert runtime.serialize_variables({"x": by_email})[0]["x"] == {
        "email": "j@example.com"
    }

    by_user_id = SearchCriteriaUserId(user_id="u-1")
    assert runtime.serialize_variables({"x": by_user_id})[0]["x"] == {"userId": "u-1"}


def test_one_of_with_nested_input_type():
    addr = AddressInput(street="Main St", city="NYC")
    action = UpdateActionSetAddress(set_address=addr)
    assert runtime.serialize_variables({"x": action})[0]["x"] == {
        "setAddress": {"street": "Main St", "city": "NYC"}
    }


def test_one_of_referenced_in_regular_input():
    criteria = SearchCriteriaName(name="John")
    wrapper = WrapperInput(criteria=criteria, limit=10)
    assert runtime.serialize_variables({"x": wrapper})[0]["x"] == {
        "criteria": {"name": "John"},
        "limit": 10,
    }


def test_one_of_single_field():
    choice = SingleChoiceValue(value="hello")
    assert runtime.serialize_variables({"x": choice})[0]["x"] == {"value": "hello"}


def test_one_of_with_enum_field():
    by_status = FilterByStatus(status="ACTIVE")
    assert runtime.serialize_variables({"x": by_status})[0]["x"] == {"status": "ACTIVE"}

    by_name = FilterByName(name="Alice")
    assert runtime.serialize_variables({"x": by_name})[0]["x"] == {"name": "Alice"}


def test_one_of_with_list_field():
    by_name = SearchByName(name="Alice")
    assert runtime.serialize_variables({"x": by_name})[0]["x"] == {"name": "Alice"}

    by_tags = SearchByTags(tags=["python", "graphql"])
    assert runtime.serialize_variables({"x": by_tags})[0]["x"] == {
        "tags": ["python", "graphql"]
    }


def test_one_of_referencing_one_of():
    inner = InnerChoiceX(x="hello")
    outer = OuterChoiceInner(inner=inner)
    assert runtime.serialize_variables({"x": outer})[0]["x"] == {
        "inner": {"x": "hello"}
    }

    outer_direct = OuterChoiceDirect(direct="world")
    assert runtime.serialize_variables({"x": outer_direct})[0]["x"] == {
        "direct": "world"
    }
