import pytest
from graphql import GraphQLResolveInfo
from pytest_httpserver import HTTPServer

from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.conftest import gql_server

generated_package(
    "interfaces_union_result",
    schema="""
    type Query {
        node(id: ID!): Node
        count: Int!
    }

    union Node = User | Admin

    type User {
        id: ID!
        name: String!
    }

    type Admin {
        id: ID!
        name: String!
        permissions: [String!]!
    }
    """,
    queries='''
    from tests.generated.interfaces_union_result.gql.api import api_gql

    get_node_and_count = api_gql(
        """
        query GetNodeAndCount($id: ID!) {
            node(id: $id) {
                __typename
                ... on User {
                    id
                    name
                }
                ... on Admin {
                    id
                    name
                    permissions
                }
            }
            count
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_union_iface_fragment",
    schema="""
    interface Node {
        id: ID!
    }

    type User implements Node {
        id: ID!
        name: String!
    }

    type Admin implements Node {
        id: ID!
        permissions: [String!]!
    }

    union Actor = User | Admin

    type Query {
        actor(id: ID!): Actor
    }
    """,
    queries='''
    from tests.generated.interfaces_union_iface_fragment.gql.api import api_gql

    get_actor = api_gql(
        """
        query GetActor($id: ID!) {
            actor(id: $id) {
                __typename
                ... on Node {
                    id
                }
                ... on User {
                    name
                }
                ... on Admin {
                    permissions
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_no_fragments",
    schema="""
    interface Node {
        id: ID!
    }

    type User implements Node {
        id: ID!
        name: String
    }

    type Post implements Node {
        id: ID!
        title: String
    }

    type Query {
        node(id: ID!): Node
    }
    """,
    queries='''
    from tests.generated.interfaces_no_fragments.gql.api import api_gql

    get_node = api_gql(
        """
        query GetNode($id: ID!) {
            node(id: $id) {
                id
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_with_fragments",
    schema="""
    interface Node {
        id: ID!
    }

    type User implements Node {
        id: ID!
        name: String
    }

    type Post implements Node {
        id: ID!
        title: String
    }

    type Comment implements Node {
        id: ID!
        body: String
    }

    type Query {
        node(id: ID!): Node
    }
    """,
    queries='''
    from tests.generated.interfaces_with_fragments.gql.api import api_gql

    get_node = api_gql(
        """
        query GetNode($id: ID!) {
            node(id: $id) {
                __typename
                id
                ... on User {
                    name
                }
                ... on Post {
                    title
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_nested",
    schema="""
    interface Child {
        id: ID!
    }

    interface Node {
        id: ID!
        child: Child
    }

    type User implements Node {
        id: ID!
        child: Child
    }

    type Post implements Node {
        id: ID!
        child: Child
    }

    type Comment implements Child {
        id: ID!
    }

    type Query {
        node(id: ID!): Node
    }
    """,
    queries='''
    from tests.generated.interfaces_nested.gql.api import api_gql

    get_node = api_gql(
        """
        query GetNode($id: ID!) {
            node(id: $id) {
                __typename
                id
                child {
                    id
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_hierarchy",
    schema="""
    interface Node {
        id: ID!
    }

    interface Entity implements Node {
        id: ID!
        createdAt: String!
    }

    type User implements Entity & Node {
        id: ID!
        createdAt: String!
        name: String
    }

    type Post implements Node {
        id: ID!
        title: String
    }

    type Query {
        node(id: ID!): Node
    }
    """,
    queries='''
    from tests.generated.interfaces_hierarchy.gql.api import api_gql

    get_node = api_gql(
        """
        query GetNode($id: ID!) {
            node(id: $id) {
                __typename
                id
                ... on Entity {
                    createdAt
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_overlapping",
    schema="""
    interface Node {
        id: ID!
    }

    interface Named {
        name: String!
    }

    type User implements Node & Named {
        id: ID!
        name: String!
    }

    type Post implements Node {
        id: ID!
    }

    type Org implements Named {
        name: String!
    }

    type Query {
        node(id: ID!): Node
    }
    """,
    queries='''
    from tests.generated.interfaces_overlapping.gql.api import api_gql

    get_node = api_gql(
        """
        query GetNode($id: ID!) {
            node(id: $id) {
                __typename
                id
                ... on Named {
                    name
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_nullable_union",
    schema="""
    type Query {
        node(id: ID!): Node
    }

    union Node = User | Admin

    type User {
        id: ID!
        name: String!
    }

    type Admin {
        id: ID!
        permissions: [String!]!
    }
    """,
    queries='''
    from tests.generated.interfaces_nullable_union.gql.api import api_gql

    get_node = api_gql(
        """
        query GetNode($id: ID!) {
            node(id: $id) {
                __typename
                ... on User {
                    id
                    name
                }
                ... on Admin {
                    id
                    permissions
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_list_union",
    schema="""
    type Query {
        nodes: [Node!]!
    }

    union Node = User | Post

    type User {
        id: ID!
        name: String!
    }

    type Post {
        id: ID!
        title: String!
    }
    """,
    queries='''
    from tests.generated.interfaces_list_union.gql.api import api_gql

    get_nodes = api_gql(
        """
        query GetNodes {
            nodes {
                __typename
                ... on User {
                    id
                    name
                }
                ... on Post {
                    id
                    title
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_named_fragment",
    schema="""
    interface Node {
        id: ID!
    }

    type User implements Node {
        id: ID!
        name: String
    }

    type Post implements Node {
        id: ID!
        title: String
    }

    type Query {
        node(id: ID!): Node
    }
    """,
    queries='''
    from tests.generated.interfaces_named_fragment.gql.api import api_gql

    user_fields = api_gql(
        """
        fragment UserFields on User {
            name
        }
        """
    )

    get_node = api_gql(
        """
        query GetNode($id: ID!) {
            node(id: $id) {
                __typename
                id
                ...UserFields
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_typename_fragment",
    schema="""
    interface Node {
        id: ID!
    }

    type User implements Node {
        id: ID!
        name: String
    }

    type Post implements Node {
        id: ID!
        title: String
    }

    type Query {
        node(id: ID!): Node
    }
    """,
    queries='''
    from tests.generated.interfaces_typename_fragment.gql.api import api_gql

    node_base = api_gql(
        """
        fragment NodeBase on Node {
            __typename
            id
        }
        """
    )

    get_node = api_gql(
        """
        query GetNode($id: ID!) {
            node(id: $id) {
                ...NodeBase
                ... on User {
                    name
                }
                ... on Post {
                    title
                }
            }
        }
        """
    )
    ''',
)

generated_package(
    "interfaces_exhaustive",
    schema="""
    interface Node {
        id: ID!
    }

    type User implements Node {
        id: ID!
        name: String
    }

    type Post implements Node {
        id: ID!
        title: String
    }
    type Query {
        node: Node
    }
    """,
    queries='''
    from tests.generated.interfaces_exhaustive.gql.api import api_gql

    get_node = api_gql(
        """
        query GetNode {
            node {
                __typename
                ... on User {
                    id
                    name
                }
                ... on Post {
                    id
                    title
                }
            }
        }
        """
    )
    ''',
)

from tests.generated.interfaces_exhaustive import queries as exhaustive_queries
from tests.generated.interfaces_hierarchy import queries as hierarchy_queries
from tests.generated.interfaces_hierarchy.gql.api import User as HierarchyUser
from tests.generated.interfaces_list_union import queries as list_union_queries
from tests.generated.interfaces_list_union.gql.api import Post as ListUnionPost
from tests.generated.interfaces_list_union.gql.api import User as ListUnionUser
from tests.generated.interfaces_named_fragment import queries as named_fragment_queries
from tests.generated.interfaces_named_fragment.gql.api import User as NamedFragmentUser
from tests.generated.interfaces_nested import queries as nested_queries
from tests.generated.interfaces_no_fragments import queries as no_fragments_queries
from tests.generated.interfaces_nullable_union import queries as nullable_union_queries
from tests.generated.interfaces_nullable_union.gql.api import Admin as NullableAdmin
from tests.generated.interfaces_nullable_union.gql.api import User as NullableUser
from tests.generated.interfaces_overlapping import queries as overlapping_queries
from tests.generated.interfaces_overlapping.gql.api import User as OverlappingUser
from tests.generated.interfaces_typename_fragment import (
    queries as typename_fragment_queries,
)
from tests.generated.interfaces_typename_fragment.gql.api import (
    Post as TypenameFragmentPost,
)
from tests.generated.interfaces_typename_fragment.gql.api import (
    User as TypenameFragmentUser,
)
from tests.generated.interfaces_union_iface_fragment import (
    queries as union_iface_queries,
)
from tests.generated.interfaces_union_iface_fragment.gql.api import (
    Admin as UnionIfaceAdmin,
)
from tests.generated.interfaces_union_iface_fragment.gql.api import (
    User as UnionIfaceUser,
)
from tests.generated.interfaces_union_result import queries as union_result_queries
from tests.generated.interfaces_with_fragments import queries as with_fragments_queries
from tests.generated.interfaces_with_fragments.gql.api import User as WithFragmentsUser


async def test_union_result_validation(httpserver: HTTPServer):
    def resolve_node(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, object]:
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Bob"}
        return {
            "__typename": "Admin",
            "id": id,
            "name": "Alice",
            "permissions": ["portal"],
        }

    def resolve_count(_root: None, _info: GraphQLResolveInfo) -> int:
        return 3

    async with gql_server(
        httpserver,
        "interfaces_union_result",
        {"Query": {"node": resolve_node, "count": resolve_count}},
    ):
        result = await union_result_queries.get_node_and_count.execute(id="user-1")
        assert result.node is not None
        assert result.count == 3


async def test_union_with_interface_fragment(httpserver: HTTPServer):
    def resolve_actor(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, object]:
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Bob"}
        return {"__typename": "Admin", "id": id, "permissions": ["portal"]}

    async with gql_server(
        httpserver,
        "interfaces_union_iface_fragment",
        {"Query": {"actor": resolve_actor}},
    ):
        user_result = await union_iface_queries.get_actor.execute(id="user-1")
        assert isinstance(user_result.actor, UnionIfaceUser)
        assert user_result.actor.id == "user-1"
        assert user_result.actor.name == "Bob"
        assert user_result.actor.model_dump() == {
            "__typename": "User",
            "id": "user-1",
            "name": "Bob",
        }

        admin_result = await union_iface_queries.get_actor.execute(id="admin-1")
        assert isinstance(admin_result.actor, UnionIfaceAdmin)
        assert admin_result.actor.id == "admin-1"
        assert admin_result.actor.permissions == ["portal"]
        assert admin_result.actor.model_dump() == {
            "__typename": "Admin",
            "id": "admin-1",
            "permissions": ["portal"],
        }


async def test_interface_without_fragments(httpserver: HTTPServer):
    def resolve_node(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Bob"}
        return {"__typename": "Post", "id": id, "title": "GraphQL 101"}

    async with gql_server(
        httpserver,
        "interfaces_no_fragments",
        {"Query": {"node": resolve_node}},
    ):
        result = await no_fragments_queries.get_node.execute(id="user-1")
        assert result.node is not None
        assert result.node.id == "user-1"


async def test_interface_with_fragments(httpserver: HTTPServer):
    def resolve_node(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Bob"}
        if id == "post-1":
            return {"__typename": "Post", "id": id, "title": "GraphQL 101"}
        return {"__typename": "Comment", "id": id, "body": "First!"}

    async with gql_server(
        httpserver,
        "interfaces_with_fragments",
        {"Query": {"node": resolve_node}},
    ):
        user_result = await with_fragments_queries.get_node.execute(id="user-1")
        assert isinstance(user_result.node, WithFragmentsUser)
        assert user_result.node.name == "Bob"
        assert user_result.node.model_dump() == {
            "__typename": "User",
            "id": "user-1",
            "name": "Bob",
        }

        comment_result = await with_fragments_queries.get_node.execute(id="comment-1")
        assert comment_result.node is not None
        assert comment_result.node.id == "comment-1"
        assert comment_result.node.model_dump() == {
            "__typename": "Comment",
            "id": "comment-1",
        }


async def test_nested_interface(httpserver: HTTPServer):
    def resolve_node(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, object]:
        return {
            "__typename": "User",
            "id": id,
            "child": {"__typename": "Comment", "id": "child-1"},
        }

    async with gql_server(
        httpserver,
        "interfaces_nested",
        {"Query": {"node": resolve_node}},
    ):
        result = await nested_queries.get_node.execute(id="user-1")
        assert result.node is not None
        assert result.node.child is not None
        assert result.node.child.id == "child-1"


async def test_interface_hierarchy(httpserver: HTTPServer):
    def resolve_node(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        if id == "user-1":
            return {
                "__typename": "User",
                "id": id,
                "createdAt": "2024-01-01",
                "name": "Bob",
            }
        return {"__typename": "Post", "id": id, "title": "GraphQL 101"}

    async with gql_server(
        httpserver,
        "interfaces_hierarchy",
        {"Query": {"node": resolve_node}},
    ):
        user_result = await hierarchy_queries.get_node.execute(id="user-1")
        assert isinstance(user_result.node, HierarchyUser)
        assert user_result.node.created_at == "2024-01-01"
        assert user_result.node.model_dump() == {
            "__typename": "User",
            "id": "user-1",
            "createdAt": "2024-01-01",
        }

        post_result = await hierarchy_queries.get_node.execute(id="post-1")
        assert post_result.node is not None
        assert post_result.node.id == "post-1"
        assert post_result.node.model_dump() == {
            "__typename": "Post",
            "id": "post-1",
        }


async def test_interface_fragment_on_overlapping_interface(httpserver: HTTPServer):
    def resolve_node(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Bob"}
        return {"__typename": "Post", "id": id}

    async with gql_server(
        httpserver,
        "interfaces_overlapping",
        {"Query": {"node": resolve_node}},
    ):
        user_result = await overlapping_queries.get_node.execute(id="user-1")
        assert isinstance(user_result.node, OverlappingUser)
        assert user_result.node.id == "user-1"
        assert user_result.node.name == "Bob"
        assert user_result.node.model_dump() == {
            "__typename": "User",
            "id": "user-1",
            "name": "Bob",
        }

        post_result = await overlapping_queries.get_node.execute(id="post-1")
        assert post_result.node is not None
        assert post_result.node.id == "post-1"
        assert post_result.node.model_dump() == {
            "__typename": "Post",
            "id": "post-1",
        }


def test_interface_fragment_requires_typename(test_project: ProjectBuilder):
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String
        }

        type Query {
            node(id: ID!): Node
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    id
                    ... on User {
                        name
                    }
                }
            }
            '''
        )
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"Missing __typename in selection set for interface 'Node'",
    ):
        test_project.generate()


def test_interface_typename_through_a_fragment_spread_twice(
    test_project: ProjectBuilder,
):
    # Both interface walks in `codegen/selection.py` -- the type conditions a
    # selection names, and whether its base carries __typename -- visit each
    # spread once per walk. `NodeIdent` is reached two ways here, directly and
    # through an inline fragment on the interface, so the second reach hits
    # that short circuit: it must drop the re-walk without dropping the
    # __typename the first reach found inside the same fragment.
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String!
        }

        type Admin implements Node {
            id: ID!
            permissions: [String!]!
        }

        type Query {
            node(id: ID!): Node
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    id
                    ...NodeIdent
                    ... on Node {
                        ...NodeIdent
                    }
                    ... on Admin {
                        permissions
                    }
                }
            }

            fragment NodeIdent on Node {
                __typename
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True


def test_typename_inside_a_concrete_type_fragment_is_not_the_base_one(
    test_project: ProjectBuilder,
):
    # `UserBits` selects __typename, but on User -- so it answers for that one
    # variant, while Admin's model would be left without the discriminator its
    # union needs. The base-__typename walk therefore steps into a spread only
    # where the fragment is conditioned on the interface itself, and this
    # document is the case that tells the two apart: rejecting it is the point
    # of the type-condition test, not of the spread being unreachable.
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String!
        }

        type Admin implements Node {
            id: ID!
            permissions: [String!]!
        }

        type Query {
            node(id: ID!): Node
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    id
                    ...UserBits
                    ... on Admin {
                        permissions
                    }
                }
            }

            fragment UserBits on User {
                __typename
                name
            }
            '''
        )
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"Missing __typename in selection set for interface 'Node'",
    ):
        test_project.generate()


def test_conditional_typename_on_polymorphic_selection_is_rejected(
    test_project: ProjectBuilder,
):
    # __typename is the pydantic discriminator of the variant union; a
    # conditional one would render an optional-Literal discriminator that
    # pydantic rejects when the generated module is imported. The rejection
    # has to come from the generator, with a diagnosis, not from the import.
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String
        }

        type Query {
            node(id: ID!): Node
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
            '''
            query GetNode($id: ID!, $x: Boolean!) {
                node(id: $id) {
                    __typename @include(if: $x)
                    id
                    ... on User {
                        name
                    }
                }
            }
            '''
        )
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"__typename .* must be selected unconditionally",
    ):
        test_project.generate()


def test_typename_reached_only_through_a_conditional_fragment_is_rejected(
    test_project: ProjectBuilder,
):
    # The same discriminator rule when the only __typename lives inside a
    # conditional inline fragment: its condition is inherited, so it does not
    # cover every state in which the variant payload exists.
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String
        }

        type Query {
            node(id: ID!): Node
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
            '''
            query GetNode($id: ID!, $x: Boolean!) {
                node(id: $id) {
                    ... @include(if: $x) { __typename }
                    id
                    ... on User {
                        name
                    }
                }
            }
            '''
        )
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"__typename .* must be selected unconditionally",
    ):
        test_project.generate()


def test_union_type_condition_inside_interface_selection_generates(
    test_project: ProjectBuilder,
):
    # `... on Media` where Media is a union overlapping the interface: a valid
    # spread (possible types intersect), so the members of the union become
    # explicit variants instead of crashing the explicit-type resolution.
    schema = """
        interface Node {
            id: ID!
        }

        type Photo implements Node {
            id: ID!
            url: String!
        }

        type Post implements Node {
            id: ID!
            title: String!
        }

        union Media = Photo

        type Query {
            node(id: ID!): Node
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    __typename
                    id
                    ... on Media {
                        ... on Photo { url }
                    }
                }
            }
            '''
        )
        """,
    )

    assert test_project.generate() is True


def test_invalid_interface_fragment_reports_error(test_project: ProjectBuilder):
    schema = """
        interface Node {
            id: ID!
        }

        type User implements Node {
            id: ID!
            name: String
        }

        type Post {
            id: ID!
            title: String
        }

        type Query {
            node(id: ID!): Node
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        get_node = api_gql(
            '''
            query GetNode($id: ID!) {
                node(id: $id) {
                    __typename
                    id
                    ... on Post {
                        title
                    }
                }
            }
            '''
        )
        """,
    )

    with pytest.raises(GraphQLGenerationError, match="Post"):
        test_project.generate()


def test_union_fragment_requires_typename(test_project: ProjectBuilder):
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
            search(q: String!): SearchResult
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            query Search($q: String!) {
                search(q: $q) {
                    ... on User {
                        id
                        name
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

    with pytest.raises(
        ValueError,
        match=r"Missing __typename in selection set for 'SearchResult'",
    ):
        test_project.generate()


def test_union_fragment_typename_in_variants(test_project: ProjectBuilder):
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
            search(q: String!): SearchResult
        }
    """

    test_project.prepare(
        schema=schema,
        queries="""
        from sample_app.gql.api import api_gql

        search = api_gql(
            '''
            query Search($q: String!) {
                search(q: $q) {
                    ... on User {
                        __typename
                        id
                        name
                    }
                    ... on Post {
                        __typename
                        id
                        title
                    }
                }
            }
            '''
        )
        """,
    )

    changed = test_project.generate()
    assert changed is True


async def test_nullable_union_result_validation(httpserver: HTTPServer):
    def resolve_node(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, object] | None:
        if id == "none":
            return None
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Bob"}
        return {"__typename": "Admin", "id": id, "permissions": ["portal"]}

    async with gql_server(
        httpserver,
        "interfaces_nullable_union",
        {"Query": {"node": resolve_node}},
    ):
        none_result = await nullable_union_queries.get_node.execute(id="none")
        assert none_result.node is None

        user_result = await nullable_union_queries.get_node.execute(id="user-1")
        assert isinstance(user_result.node, NullableUser)
        assert user_result.node.name == "Bob"
        assert user_result.node.model_dump() == {
            "__typename": "User",
            "id": "user-1",
            "name": "Bob",
        }

        admin_result = await nullable_union_queries.get_node.execute(id="admin-1")
        assert isinstance(admin_result.node, NullableAdmin)
        assert admin_result.node.permissions == ["portal"]
        assert admin_result.node.model_dump() == {
            "__typename": "Admin",
            "id": "admin-1",
            "permissions": ["portal"],
        }


async def test_list_wrapped_union(httpserver: HTTPServer):
    def resolve_nodes(_root: None, _info: GraphQLResolveInfo) -> list[dict[str, str]]:
        return [
            {"__typename": "User", "id": "u-1", "name": "Bob"},
            {"__typename": "Post", "id": "p-1", "title": "GraphQL 101"},
        ]

    async with gql_server(
        httpserver,
        "interfaces_list_union",
        {"Query": {"nodes": resolve_nodes}},
    ):
        result = await list_union_queries.get_nodes.execute()
        assert len(result.nodes) == 2
        assert isinstance(result.nodes[0], ListUnionUser)
        assert result.nodes[0].name == "Bob"
        assert result.nodes[0].model_dump() == {
            "__typename": "User",
            "id": "u-1",
            "name": "Bob",
        }
        assert isinstance(result.nodes[1], ListUnionPost)
        assert result.nodes[1].title == "GraphQL 101"
        assert result.nodes[1].model_dump() == {
            "__typename": "Post",
            "id": "p-1",
            "title": "GraphQL 101",
        }


async def test_interface_with_named_fragment_type_condition(httpserver: HTTPServer):
    def resolve_node(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Bob"}
        return {"__typename": "Post", "id": id, "title": "GraphQL 101"}

    async with gql_server(
        httpserver,
        "interfaces_named_fragment",
        {"Query": {"node": resolve_node}},
    ):
        user_result = await named_fragment_queries.get_node.execute(id="user-1")
        assert isinstance(user_result.node, NamedFragmentUser)
        assert user_result.node.name == "Bob"
        assert user_result.node.model_dump() == {
            "__typename": "User",
            "id": "user-1",
            "name": "Bob",
        }

        post_result = await named_fragment_queries.get_node.execute(id="post-1")
        assert post_result.node is not None
        assert post_result.node.id == "post-1"
        assert post_result.node.model_dump() == {
            "__typename": "Post",
            "id": "post-1",
        }


async def test_interface_typename_in_named_fragment(httpserver: HTTPServer):
    def resolve_node(
        _root: None, _info: GraphQLResolveInfo, *, id: str
    ) -> dict[str, str]:
        if id == "user-1":
            return {"__typename": "User", "id": id, "name": "Bob"}
        return {"__typename": "Post", "id": id, "title": "GraphQL 101"}

    async with gql_server(
        httpserver,
        "interfaces_typename_fragment",
        {"Query": {"node": resolve_node}},
    ):
        user_result = await typename_fragment_queries.get_node.execute(id="user-1")
        assert isinstance(user_result.node, TypenameFragmentUser)
        assert user_result.node.name == "Bob"
        assert user_result.node.model_dump() == {
            "__typename": "User",
            "id": "user-1",
            "name": "Bob",
        }

        post_result = await typename_fragment_queries.get_node.execute(id="post-1")
        assert isinstance(post_result.node, TypenameFragmentPost)
        assert post_result.node.title == "GraphQL 101"
        assert post_result.node.model_dump() == {
            "__typename": "Post",
            "id": "post-1",
            "title": "GraphQL 101",
        }


async def test_interface_exhaustively_covered(httpserver: HTTPServer):
    calls = 0

    def resolve_node(_root: None, _info: GraphQLResolveInfo) -> dict[str, str]:
        nonlocal calls
        if calls == 0:
            calls += 1
            return {"__typename": "User", "id": "user-1", "name": "Bob"}
        return {"__typename": "Post", "id": "post-1", "title": "GraphQL 101"}

    async with gql_server(
        httpserver,
        "interfaces_exhaustive",
        {"Query": {"node": resolve_node}},
    ):
        user_result = await exhaustive_queries.get_node.execute()
        assert user_result.node is not None
        assert user_result.node.model_dump() == {
            "__typename": "User",
            "id": "user-1",
            "name": "Bob",
        }

        post_result = await exhaustive_queries.get_node.execute()
        assert post_result.node is not None
        assert post_result.node.model_dump() == {
            "__typename": "Post",
            "id": "post-1",
            "title": "GraphQL 101",
        }
