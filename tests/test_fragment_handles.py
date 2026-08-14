from types import ModuleType

import pydantic
import pytest

from iron_gql import runtime
from iron_gql import slots
from iron_gql.codegen import GraphQLGenerationError
from tests.conftest import ProjectBuilder
from tests.conftest import generated_package
from tests.conftest import generated_source

SCHEMA = """
type Query {
    node(id: ID!): Node
    viewer: User!
}

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
"""

generated_package(
    "fragment_handles",
    schema=SCHEMA,
    queries='''
    from tests.generated.fragment_handles.gql.api import api_gql

    user_fields = api_gql(
        """
        fragment UserFields on User {
            id
            name
        }
        """
    )

    node_fields = api_gql(
        """
        fragment NodeFields on Node {
            __typename
            id
            ... on Admin { permissions }
        }
        """
    )

    combined = api_gql(
        """
        fragment ViewerFields on User {
            name
        }

        query GetViewer {
            viewer {
                id
                ...ViewerFields
            }
        }
        """
    )

    with_slot = api_gql(
        """
        query WithSlot($id: ID!) {
            node(id: $id) @slot { __typename }
        }
        """
    )

    # Both fragments are typed definitions because the package holds a template at all;
    # these two binds are here for the bound operations the tests below read
    # through, not to make the definitions exist.
    with_user_fields = with_slot.bind(node=user_fields)
    with_node_fields = with_slot.bind(node=node_fields)
    ''',
)

from tests.generated.fragment_handles import queries as fragment_queries
from tests.generated.fragment_handles.gql import api


def test_fragment_statement_returns_a_new_definition_value():
    first = api.api_gql(
        """
    fragment UserFields on User {
        id
        name
    }
    """
    )
    second = api.api_gql(
        """
    fragment UserFields on User {
        id
        name
    }
    """
    )
    assert isinstance(first, api.UserFields)
    assert isinstance(second, api.UserFields)
    assert first is not second


def test_fragment_class_declares_its_on_type_base_and_closure():
    # Public definition class — обычный concrete value type. `api_gql()`
    # хранит сам class и создаёт новый instance для каждого вызова.
    source = generated_source("fragment_handles")
    assert "class OnUser(slots.GQLBindableFragment[TModel, TReads], ABC):" in source
    assert '@final\nclass UserFields(OnUser[UserFieldsData, "UserFields"]):' in source
    assert "class _UserFields(UserFields):" not in source
    assert "USER_FIELDS" not in source
    assert "FRAGMENTS_BY_NAME" not in source


NARROWING_SCHEMA = """
type Query {
    imageAttachment(id: ID!): ImageAttachment
    linkAttachment(id: ID!): LinkAttachment
}

interface Attachment {
    id: ID!
}

type ImageAttachment implements Attachment {
    id: ID!
    url: String!
}

type LinkAttachment implements Attachment {
    id: ID!
    href: String!
}
"""


def test_closure_narrows_to_the_intersection_over_every_compatible_slot_type(
    test_project: ProjectBuilder,
):
    # `NodeParts` (on the `Attachment` interface) spreads `ImageBits` inside an
    # `... on ImageAttachment` inline fragment -- reachable at the package's
    # only slot in `test_fragment_class_declares_its_on_type_base_and_closure`
    # above, where the closure comes out as the full union. Here the package
    # carries a *second* slot, of the disjoint type `LinkAttachment`: at that
    # slot `ImageBits` is reachable on paper (`NodeParts` is still
    # spread-compatible, through `Attachment`) but at no typename the slot can
    # actually hold, so `readable_fragments` drops it there. The closure
    # written into `NodeParts`'s own base is the *intersection* over both
    # slots, not just the one it happens to be bound into, so `ImageBits` must
    # исчезнуть из него. Это доказывает narrowing на нескольких slot types, а
    # не no-op случай пакета с единственным slot type.
    test_project.prepare(
        schema=NARROWING_SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        image_bits = api_gql(
            '''
            fragment ImageBits on ImageAttachment {
                url
            }
            '''
        )

        node_parts = api_gql(
            '''
            fragment NodeParts on Attachment {
                __typename
                id
                ... on ImageAttachment {
                    ...ImageBits
                }
            }
            '''
        )

        get_image = api_gql(
            '''
            query GetImage($id: ID!) {
                imageAttachment(id: $id) @slot { __typename }
            }
            '''
        )

        get_link = api_gql(
            '''
            query GetLink($id: ID!) {
                linkAttachment(id: $id) @slot { __typename }
            }
            '''
        )

        bound = get_image.bind(image_attachment=node_parts)
        """,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    # `ImageBits` is a class of the package all the same -- the narrowing is
    # about what `NodeParts` may promise, not about which fragments exist --
    # so its absence from the closure below is a fact about that promise and
    # not about a fragment the generator happened to drop.
    assert (
        '@final\nclass ImageBits(OnImageAttachment[ImageBitsData, "ImageBits"]):'
        in generated
    )
    assert (
        '@final\nclass NodeParts(OnAttachment[NodePartsData, "NodeParts"]):'
        in generated
    )


def test_definition_carries_its_fragment_name():
    assert fragment_queries.user_fields.fragment_name__ == "UserFields"


def test_definition_validates_its_own_selection():
    data = api.UserFields().validate__({"id": "u-1", "name": "Alice"})
    assert isinstance(data, api.UserFieldsData)
    assert data.name == "Alice"


def test_definition_model_ignores_other_readers_fields():
    # The payload a definition validates carries the other passed fragments'
    # fields next to its own; the model keeps exactly its own selection.
    data = api.UserFields().validate__({
        "id": "u-1",
        "name": "Alice",
        "email": "alice@example.com",
    })
    assert data.model_dump() == {"id": "u-1", "name": "Alice"}


def test_interface_fragment_covers_every_possible_type():
    # Every possible type is covered, and each variant model sees only its own
    # selection: `permissions` belongs to the Admin inline fragment and is not
    # a field of the User variant. `slot_readers` is an instance attribute
    # now, built at `bind()` time (`bound__`) rather than a per-combination
    # `ClassVar`.
    (reader,) = fragment_queries.with_node_fields.slot_readers["node"]
    assert reader.typenames == frozenset({"Admin", "User"})
    user = api.NodeFields().validate__({
        "__typename": "User",
        "id": "u-1",
        "permissions": ["root"],
    })
    assert "permissions" not in user.model_dump()


def test_interface_fragment_model_is_a_discriminated_union():
    admin = api.NodeFields().validate__({
        "__typename": "Admin",
        "id": "a-1",
        "permissions": ["root"],
    })
    assert isinstance(admin, pydantic.BaseModel)
    assert admin.model_dump(by_alias=True) == {
        "__typename": "Admin",
        "id": "a-1",
        "permissions": ["root"],
    }

    user = api.NodeFields().validate__({
        "__typename": "User",
        "id": "u-1",
    })
    assert isinstance(user, pydantic.BaseModel)
    assert user.model_dump(by_alias=True) == {"__typename": "User", "id": "u-1"}


def test_statement_with_an_operation_is_not_a_definition():
    assert isinstance(fragment_queries.combined, api.GetViewer)
    assert not isinstance(fragment_queries.combined, slots.GQLFragment)
    assert not hasattr(api, "ViewerFields")


def test_package_without_operations_passes_fragments_through(
    test_project: ProjectBuilder,
):
    # No operations means no templates, so nothing can bind it — the
    # statement keeps its pre-bind meaning: spread by name, untyped catch-all.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        only_user = api_gql(
            '''
            fragment OnlyUser on User {
                id
            }
            '''
        )
        """,
    )
    _api_module, queries_module = test_project.generate_and_import()
    assert isinstance(queries_module.only_user, runtime.GQLOperation)  # pyright: ignore[reportAny]


def test_duplicate_fragment_names_across_statements_are_rejected(
    test_project: ProjectBuilder,
):
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        first = api_gql(
            '''
            fragment UserFields on User {
                id
            }
            '''
        )

        second = api_gql(
            '''
            fragment UserFields on User {
                name
            }
            '''
        )

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )

        # The template is what makes the two fragments typed definitions: a
        # package with no template compiles no fragment into one, so the name
        # collision would never surface.
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"^Cannot compile different GraphQL fragments with same name UserFields",
    ):
        test_project.generate()


NESTED_HANDLE_SCHEMA = """
type Query {
    node(id: ID!): Node
    profile(id: ID!): Profile
}

interface Node {
    id: ID!
}

type User implements Node {
    id: ID!
    profile: Profile!
}

type Profile {
    handle: String!
    bio: String!
}
"""

NESTED_HANDLE_STATEMENTS = """
        card = api_gql(
            '''
            fragment Card on User {
                __typename
                id
                profile { handle }
            }
            '''
        )

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )

        bound = with_slot.bind(node=card)
"""


def _nested_model_name(api_module: ModuleType) -> str:
    profile_field = api_module.CardData.model_fields["profile"]  # pyright: ignore[reportAny]
    annotation = profile_field.annotation  # pyright: ignore[reportAny]
    assert isinstance(annotation, type)
    return annotation.__name__


def test_definition_model_names_ignore_unrelated_operations(
    test_project: ProjectBuilder,
):
    # A definition's model names — the root and everything reachable from it —
    # are public API: callers annotate and narrow against them. Adding an
    # unrelated operation that selects the same GraphQL type differently
    # must not move any of them.
    test_project.prepare(
        schema=NESTED_HANDLE_SCHEMA,
        queries=f"""
        from sample_app.gql.api import api_gql
        {NESTED_HANDLE_STATEMENTS}
        """,
    )
    api_module, _queries_module = test_project.generate_and_import()
    baseline = _nested_model_name(api_module)

    test_project.prepare(
        schema=NESTED_HANDLE_SCHEMA,
        queries=f"""
        from sample_app.gql.api import api_gql
        {NESTED_HANDLE_STATEMENTS}
        unrelated = api_gql(
            '''
            query Unrelated {{
                profile(id: "1") {{ bio }}
            }}
            '''
        )
        """,
    )
    api_module, _queries_module = test_project.generate_and_import()
    assert _nested_model_name(api_module) == baseline


def test_local_shadowing_resolves_spreads_local_first(
    test_project: ProjectBuilder,
):
    # The operation defines and spreads its own `Bits`; a different bundle
    # defines a same-named `Bits` that spreads `Extra`, which is ambiguous
    # across statements. The operation never reaches that `Extra`: its spread
    # resolves local-first, the same way `make_validation_doc` builds the
    # document that is validated and sent.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query Q($id: ID!) {
                node(id: $id) { ...Bits }
            }
            fragment Bits on Node { id }
            '''
        )

        other_bundle = api_gql(
            '''
            fragment Bits on Node { ...Extra }
            fragment Extra on Node { id }
            '''
        )

        another_extra = api_gql(
            '''
            fragment Extra on Node { __typename }
            '''
        )
        """,
    )
    assert test_project.generate() is True


def test_duplicate_fragment_with_different_spelling_returns_new_definitions(
    test_project: ProjectBuilder,
):
    # Deduplication compares dedented text, so the same fragment indented
    # differently at two call sites is one definition type — but the statement
    # factory table is keyed by the exact literal, so every spelling must resolve to the
    # generated definition class rather than fall through to a bare
    # GQLOperation.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        first = api_gql("fragment UserBits on User { id }")
        second = api_gql('''
            fragment UserBits on User { id }
        ''')

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )

        bound = with_slot.bind(node=first)
        """,
    )
    api_module, queries_module = test_project.generate_and_import()
    assert isinstance(queries_module.first, api_module.UserBits)  # pyright: ignore[reportAny]
    assert isinstance(queries_module.second, api_module.UserBits)  # pyright: ignore[reportAny]
    assert queries_module.first is not queries_module.second  # pyright: ignore[reportAny]


def test_fragment_pinning_a_generated_model_name_is_rejected(
    test_project: ProjectBuilder,
):
    # Fragment `qResultUser` pins `QResultUserData` — the same raw name the
    # path `q.user.data` generates for its model. The rename map is keyed by
    # name, so it cannot move one while keeping the other; the collision has
    # to be resolved by the developer.
    test_project.prepare(
        schema="""
        type Query {
            user: User
        }

        type User {
            name: String
            data: Data
        }

        type Data {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query q { user { data { id } } }
            '''
        )

        f = api_gql(
            '''
            fragment qResultUser on User { name }
            '''
        )

        s = api_gql(
            '''
            query S { user @slot { __typename } }
            '''
        )

        bound = s.bind(user=f)
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="pins model name"):
        test_project.generate()


def test_fragment_named_python_keyword_is_rejected(test_project: ProjectBuilder):
    # `fragment none` is valid GraphQL, but capitalize_first turns it into
    # `class None(...)` — the module must be rejected before it is written,
    # not discovered broken at import.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        f = api_gql(
            '''
            fragment none on User { id }
            '''
        )

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )

        bound = with_slot.bind(node=f)
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match=r"'None'.*not a usable Python identifier"
    ):
        test_project.generate()


def test_statically_empty_fragment_is_rejected(test_project: ProjectBuilder):
    # A literal `@skip(if: true)` on every field leaves the fragment model
    # without fields, and a fieldless class renders with an empty body that
    # the generated module cannot even import.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        empty_bits = api_gql(
            '''
            fragment EmptyBits on User {
                id @skip(if: true)
            }
            '''
        )

        with_slot = api_gql(
            '''
            query WithSlot($id: ID!) {
                node(id: $id) @slot { __typename }
            }
            '''
        )

        bound = with_slot.bind(node=empty_bits)
        """,
    )
    with pytest.raises(ValueError, match="statically empty"):
        test_project.generate()


def test_invalid_standalone_fragment_is_rejected(test_project: ProjectBuilder):
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        broken = api_gql(
            '''
            fragment UserFields on User {
                id
                nickname
            }
            '''
        )
        """,
    )

    with pytest.raises(GraphQLGenerationError, match="Cannot query field 'nickname'"):
        test_project.generate()


def test_unknown_statement_is_rejected_instead_of_a_bare_operation():
    # A stale generated module fed a statement it does not know used to hand
    # back a bare GQLOperation whose first use failed far from the cause; the
    # lookup now names the actual problem.
    with pytest.raises(LookupError, match="regenerate the package"):
        api.api_gql("query Unknown { viewer { id } }")


def test_operation_spreading_an_ambiguous_fragment_is_rejected(
    test_project: ProjectBuilder,
):
    # The operation defines no local `Common`, so it resolves through the
    # global fragment index where scan order picks the winner — same ambiguity rule as
    # a definition's dependencies.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        one = api_gql(
            '''
            fragment Common on User { name }

            query UseOne {
                viewer { id ...Common }
            }
            '''
        )

        other = api_gql("fragment Common on User { id }")

        use = api_gql(
            '''
            query UseOther {
                viewer { ...Common }
            }
            '''
        )
        """,
    )
    with pytest.raises(
        GraphQLGenerationError, match=r"spreads fragment 'Common', which is defined"
    ):
        test_project.generate()


def test_bundle_statement_returns_the_untyped_catch_all(
    test_project: ProjectBuilder,
):
    # README: a statement bundling several fragments without an operation
    # keeps returning the untyped catch-all — its fragments are spread
    # statically by name, and the call sits at module level, so a raise here
    # would break the import of user code. The other case that stays untyped
    # — a single fragment in a package with no template at all — is pinned by
    # test_package_without_operations_passes_fragments_through above.
    test_project.prepare(
        schema=SCHEMA,
        queries="""
        from sample_app.gql.api import api_gql

        bundle = api_gql(
            '''
            fragment A on User { id }

            fragment B on User { name }

            query UseBoth {
                viewer { ...A ...B }
            }
            '''
        )

        fragments_only = api_gql(
            '''
            fragment C on User { id }

            fragment D on User { name }
            '''
        )
        """,
    )
    _api_module, queries_module = test_project.generate_and_import()
    assert isinstance(queries_module.fragments_only, runtime.GQLOperation)  # pyright: ignore[reportAny]
