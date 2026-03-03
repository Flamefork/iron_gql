import warnings

import pytest

from iron_gql.codegen import GraphQLDeprecationWarning
from tests.conftest import ProjectBuilder


def test_deprecated_result_field_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }

        type User {
            id: ID!
            name: String @deprecated(reason: "Use fullName instead")
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            query GetUser {
                user {
                    id
                    name
                }
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=(
            r"Query 'GetUser': field 'User\.name'"
            r" is deprecated: Use fullName instead"
        ),
    ):
        test_project.generate()


def test_deprecated_argument_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            users(
                limit: Int
                first: Int @deprecated(reason: "Use limit instead")
            ): [String]
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_users = api_gql(
            '''
            query GetUsers($first: Int) {
                users(first: $first)
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=(
            r"Query 'GetUsers': argument 'first'"
            r" on 'Query\.users' is deprecated:"
            r" Use limit instead"
        ),
    ):
        test_project.generate()


def test_deprecated_input_field_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            update(input: UpdateInput!): Boolean
        }

        input UpdateInput {
            name: String!
            legacyName: String @deprecated(reason: "Use name instead")
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        update = api_gql(
            '''
            mutation Update($input: UpdateInput!) {
                update(input: $input)
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=r"Input field 'UpdateInput\.legacyName' is deprecated: Use name instead",
    ):
        test_project.generate()


def test_deprecated_enum_value_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            status: Status
        }

        enum Status {
            ACTIVE
            INACTIVE @deprecated(reason: "Use DISABLED instead")
            DISABLED
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_status = api_gql(
            '''
            query GetStatus {
                status
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=r"Enum value 'Status\.INACTIVE' is deprecated: Use DISABLED instead",
    ):
        test_project.generate()


def test_deprecated_one_of_input_field_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            ping: Boolean
        }

        type Mutation {
            search(criteria: SearchCriteria!): Boolean
        }

        input SearchCriteria @oneOf {
            name: String
            legacyId: ID @deprecated(reason: "Use name instead")
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

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=r"Input field 'SearchCriteria\.legacyId' is deprecated: Use name instead",
    ):
        test_project.generate()


def test_deprecated_field_without_reason(test_project: ProjectBuilder):
    schema = """
        type Query {
            user: User
        }

        type User {
            id: ID!
            oldField: String @deprecated
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_user = api_gql(
            '''
            query GetUser {
                user {
                    id
                    oldField
                }
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=(
            r"Query 'GetUser': field 'User\.oldField'"
            r" is deprecated: No longer supported"
        ),
    ):
        test_project.generate()


def test_deprecated_field_in_union(test_project: ProjectBuilder):
    schema = """
        type Query {
            search: SearchResult
        }

        union SearchResult = User | Post

        type User {
            id: ID!
            legacyName: String @deprecated(reason: "Use name instead")
        }

        type Post {
            id: ID!
            title: String
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
                    ... on User {
                        id
                        legacyName
                    }
                    ... on Post {
                        id
                        title
                    }
                }
            }
            '''
        )
        """,
    )

    with pytest.warns(
        GraphQLDeprecationWarning,
        match=(
            r"Query 'Search': field 'User\.legacyName'"
            r" is deprecated: Use name instead"
        ),
    ):
        test_project.generate()


def test_deprecated_argument_not_used_no_warning(test_project: ProjectBuilder):
    schema = """
        type Query {
            users(
                limit: Int
                first: Int @deprecated(reason: "Use limit instead")
            ): [String]
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_users = api_gql(
            '''
            query GetUsers($limit: Int) {
                users(limit: $limit)
            }
            '''
        )
        """,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", GraphQLDeprecationWarning)
        test_project.generate()


def test_no_deprecated_no_warnings(test_project: ProjectBuilder):
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

        get_user = api_gql(
            '''
            query GetUser {
                user {
                    id
                    name
                }
            }
            '''
        )
        """,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", GraphQLDeprecationWarning)
        test_project.generate()
