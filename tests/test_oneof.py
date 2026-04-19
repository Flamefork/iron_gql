from iron_gql import runtime
from tests.conftest import ProjectBuilder


def test_one_of_basic_scalar_fields(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            search(criteria: SearchCriteria!): Boolean
        }

        input SearchCriteria @oneOf {
            name: String
            email: String
            userId: ID
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            mutation Search($criteria: SearchCriteria!) {
                search(criteria: $criteria)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    by_name = api.SearchCriteriaName(name="John")
    assert runtime.serialize_variables({"x": by_name})[0]["x"] == {"name": "John"}

    by_email = api.SearchCriteriaEmail(email="j@example.com")
    assert runtime.serialize_variables({"x": by_email})[0]["x"] == {
        "email": "j@example.com"
    }

    by_user_id = api.SearchCriteriaUserId(user_id="u-1")
    assert runtime.serialize_variables({"x": by_user_id})[0]["x"] == {"userId": "u-1"}


def test_one_of_with_nested_input_type(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            update(input: UpdateAction!): Boolean
        }

        input AddressInput {
            street: String!
            city: String!
        }

        input UpdateAction @oneOf {
            setName: String
            setAddress: AddressInput
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        update = api_gql(
            '''
            mutation Update($input: UpdateAction!) {
                update(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    addr = api.AddressInput(street="Main St", city="NYC")
    action = api.UpdateActionSetAddress(set_address=addr)
    assert runtime.serialize_variables({"x": action})[0]["x"] == {
        "setAddress": {"street": "Main St", "city": "NYC"}
    }


def test_one_of_referenced_in_regular_input(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            doSearch(input: WrapperInput!): Boolean
        }

        input SearchCriteria @oneOf {
            name: String
            email: String
        }

        input WrapperInput {
            criteria: SearchCriteria!
            limit: Int
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        do_search = api_gql(
            '''
            mutation DoSearch($input: WrapperInput!) {
                doSearch(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    criteria = api.SearchCriteriaName(name="John")
    wrapper = api.WrapperInput(criteria=criteria, limit=10)
    assert runtime.serialize_variables({"x": wrapper})[0]["x"] == {
        "criteria": {"name": "John"},
        "limit": 10,
    }


def test_one_of_single_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            act(input: SingleChoice!): Boolean
        }

        input SingleChoice @oneOf {
            value: String
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        act = api_gql(
            '''
            mutation Act($input: SingleChoice!) {
                act(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    choice = api.SingleChoiceValue(value="hello")
    assert runtime.serialize_variables({"x": choice})[0]["x"] == {"value": "hello"}


def test_one_of_with_enum_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            filter(by: FilterBy!): Boolean
        }

        enum Status {
            ACTIVE
            INACTIVE
        }

        input FilterBy @oneOf {
            status: Status
            name: String
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        do_filter = api_gql(
            '''
            mutation Filter($by: FilterBy!) {
                filter(by: $by)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    by_status = api.FilterByStatus(status="ACTIVE")
    assert runtime.serialize_variables({"x": by_status})[0]["x"] == {"status": "ACTIVE"}

    by_name = api.FilterByName(name="Alice")
    assert runtime.serialize_variables({"x": by_name})[0]["x"] == {"name": "Alice"}


def test_one_of_with_list_field(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            search(by: SearchBy!): Boolean
        }

        input SearchBy @oneOf {
            name: String
            tags: [String!]
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            mutation Search($by: SearchBy!) {
                search(by: $by)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    by_name = api.SearchByName(name="Alice")
    assert runtime.serialize_variables({"x": by_name})[0]["x"] == {"name": "Alice"}

    by_tags = api.SearchByTags(tags=["python", "graphql"])
    assert runtime.serialize_variables({"x": by_tags})[0]["x"] == {
        "tags": ["python", "graphql"]
    }


def test_one_of_referencing_one_of(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            act(input: OuterChoice!): Boolean
        }

        input InnerChoice @oneOf {
            x: String
            y: Int
        }

        input OuterChoice @oneOf {
            inner: InnerChoice
            direct: String
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        act = api_gql(
            '''
            mutation Act($input: OuterChoice!) {
                act(input: $input)
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True

    api = test_project.import_api()
    inner = api.InnerChoiceX(x="hello")
    outer = api.OuterChoiceInner(inner=inner)
    assert runtime.serialize_variables({"x": outer})[0]["x"] == {
        "inner": {"x": "hello"}
    }

    outer_direct = api.OuterChoiceDirect(direct="world")
    assert runtime.serialize_variables({"x": outer_direct})[0]["x"] == {
        "direct": "world"
    }
