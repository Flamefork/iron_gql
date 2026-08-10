import pytest

from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.naming import apply_rename
from iron_gql.codegen.naming import build_rename_map
from tests.conftest import ProjectBuilder


def _field(name: str, type_info: TypeRef) -> CollectedField:
    return CollectedField(name=name, response_key=name, type_info=type_info)


def _scalar(expr: str) -> ScalarRef:
    return ScalarRef(expr=expr)


def test_single_form_promoted_to_graphql_type_name():
    # One collected form per GraphQL type → short name is promoted
    # back to the GraphQL type name.
    foo = CollectedModel(
        name="Foo_1",
        graphql_type_name="Foo",
        fields=[_field("a", _scalar("str")), _field("b", _scalar("int"))],
    )
    rename = build_rename_map([foo], frozenset(), frozenset())
    assert rename == {"Foo_1": "Foo", "FooWithAB": "Foo"}


def test_same_shape_deduplicates():
    # Two models with identical shape_key collapse onto the same candidate.
    shape_fields = [_field("a", _scalar("str")), _field("b", _scalar("int"))]
    first = CollectedModel(name="Foo_1", graphql_type_name="Foo", fields=shape_fields)
    second = CollectedModel(
        name="Foo_2", graphql_type_name="Foo", fields=list(shape_fields)
    )
    rename = build_rename_map([first, second], frozenset(), frozenset())
    # Both collapse to the graphql type name (single variant in type_variants).
    assert rename["Foo_1"] == "Foo"
    assert rename["Foo_2"] == "Foo"


def test_collision_on_two_forms_separated_by_named_tokens():
    # Same field names (so candidate collides), different referenced types →
    # two distinct shapes get detailed suffixes from type_name_tokens.
    first = CollectedModel(
        name="Foo_1",
        graphql_type_name="Foo",
        fields=[_field("x", NamedRef(name="Alpha"))],
    )
    second = CollectedModel(
        name="Foo_2",
        graphql_type_name="Foo",
        fields=[_field("x", NamedRef(name="Beta"))],
    )
    rename = build_rename_map([first, second], frozenset(), frozenset())
    assert rename["Foo_1"] == "FooWithX_Alpha"
    assert rename["Foo_2"] == "FooWithX_Beta"


def test_three_forms_all_get_detailed_suffix():
    first = CollectedModel(
        name="Foo_1",
        graphql_type_name="Foo",
        fields=[_field("x", NamedRef(name="Alpha"))],
    )
    second = CollectedModel(
        name="Foo_2",
        graphql_type_name="Foo",
        fields=[_field("x", NamedRef(name="Beta"))],
    )
    third = CollectedModel(
        name="Foo_3",
        graphql_type_name="Foo",
        fields=[_field("x", NamedRef(name="Gamma"))],
    )
    rename = build_rename_map([first, second, third], frozenset(), frozenset())
    assert rename["Foo_1"] == "FooWithX_Alpha"
    assert rename["Foo_2"] == "FooWithX_Beta"
    assert rename["Foo_3"] == "FooWithX_Gamma"


def test_single_form_blocked_by_reserved_name_keeps_short_name():
    # An unrelated artifact already occupies the desired GraphQL type name.
    # The final promotion step must leave the short form intact.
    reserved = CollectedUnionAlias(name="Foo", variants=("Alpha", "Beta"))
    foo = CollectedModel(
        name="Foo_1",
        graphql_type_name="Foo",
        fields=[_field("a", _scalar("str"))],
    )
    rename = build_rename_map([reserved, foo], frozenset(), frozenset())
    assert rename["Foo_1"] == "FooWithA"
    assert "Foo" not in rename.values()


def test_different_graphql_types_are_independent():
    foo = CollectedModel(
        name="Foo_1",
        graphql_type_name="Foo",
        fields=[_field("a", _scalar("str"))],
    )
    bar = CollectedModel(
        name="Bar_1",
        graphql_type_name="Bar",
        fields=[_field("a", _scalar("str"))],
    )
    rename = build_rename_map([foo, bar], frozenset(), frozenset())
    assert rename["Foo_1"] == "Foo"
    assert rename["Bar_1"] == "Bar"


def _pkg(artifacts: list[CollectedModel | CollectedUnionAlias]) -> CollectedPackageIR:
    return CollectedPackageIR(
        result_artifacts=list(artifacts),
        binding_artifacts=[],
        input_artifacts=[],
        operations=[],
        fragments=[],
        templates=[],
        bindings=[],
        enums=[],
        open_model_names=frozenset(),
        discovered_texts=(),
    )


def test_two_selections_deriving_one_name_are_diagnosed(
    test_project: ProjectBuilder,
):
    # The detailed name concatenates field names and type tokens with nothing
    # between them, and `field_name_to_pascal` strips underscores -- so
    # `{a_b, c}` and `{a, b_c}` both spell `ABC`, and two different shapes of
    # `T` ask for one class. Ordinary schema, ordinary queries: the generator
    # cannot name them apart, and that is the user's to hear rather than a
    # crash marked unreachable.
    test_project.prepare(
        schema="""
        type T { a_b: String, c: String, a: String, b_c: String }
        type Query { t: T }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q1 = api_gql("query Q1 { t { a_b c } }")
        q2 = api_gql("query Q2 { t { a b_c } }")
        """,
    )
    with pytest.raises(GraphQLGenerationError, match="derive the same generated name"):
        _ = test_project.generate()


def test_apply_rename_collapses_identical_shapes():
    # Two sibling models with identical shape and the same target name should
    # produce a single deduplicated model, not an assertion failure.
    shape = [_field("a", _scalar("str"))]
    first = CollectedModel(name="Foo_1", graphql_type_name="Foo", fields=list(shape))
    second = CollectedModel(name="Foo_2", graphql_type_name="Foo", fields=list(shape))
    ir = _pkg([first, second])

    result = apply_rename(ir, frozenset())

    models = [a for a in result.result_artifacts if isinstance(a, CollectedModel)]
    assert [m.name for m in models] == ["Foo"]


def test_rename_map_is_order_independent_for_same_input_set():
    # Permuting the input artifact list must not change the resulting name
    # assignments — rename is a function of the *set* of models, not order.
    a = CollectedModel(
        name="Foo_1",
        graphql_type_name="Foo",
        fields=[_field("x", NamedRef(name="Alpha"))],
    )
    b = CollectedModel(
        name="Foo_2",
        graphql_type_name="Foo",
        fields=[_field("x", NamedRef(name="Beta"))],
    )
    first = build_rename_map([a, b], frozenset(), frozenset())
    second = build_rename_map([b, a], frozenset(), frozenset())
    assert first == second


def test_generated_name_colliding_with_a_fixed_name_is_diagnosed(
    test_project: ProjectBuilder,
):
    # The residual the two collapse tests below cannot cover: both phases yield
    # to every fixed name, but the *detailed* Phase A name is assigned before
    # them and answers to nothing, so a fixed artifact spelled exactly like one
    # is a real collision and the only honest answer is a diagnosis.
    #
    # Reached with an operation named after the model its own selection
    # generates: `Post`, field `id`, and the tokens of that field's
    # rendered type (`PostWithId_Opt_String`). The
    # aliased `id: child` is what keeps the collapse phases off that model: two
    # selections of `Post` then share the field name `id` with different
    # shapes, so neither the short form nor the bare type name is taken.
    test_project.prepare(
        schema="""
        type Query {
            post: Post
        }

        type Post {
            id: String
            child: Post
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql("query PostWithId_Opt_String { post { id: child { id } } }")
        """,
    )
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()

    [error] = exc_info.value.errors
    assert "Generated model name(s) 'PostWithId_Opt_String' collide" in error
    assert "alias the colliding field or rename the fragment" in error


def test_short_name_collapse_yields_to_a_slot_model_name(
    test_project: ProjectBuilder,
):
    # `withItem @slot` fixes QResultWithItemSlot; the model for `thing` (type
    # QResult, field itemSlot) would collapse to the same short name. The
    # collapse must yield to the fixed name instead of crashing the rename
    # pass with an internal assertion.
    test_project.prepare(
        schema="""
        type Query {
            withItem: Item
            thing: QResult
        }

        type Item {
            id: ID!
        }

        type QResult {
            itemSlot: String
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        q = api_gql(
            '''
            query Q {
                withItem @slot { __typename }
                thing { itemSlot }
            }
            '''
        )
        """,
    )
    assert test_project.generate() is True
    test_project.import_api()


def test_short_name_collapse_yields_to_a_pinned_fragment_model(
    test_project: ProjectBuilder,
):
    # `fragment TWith` pins TWithData; the model for `thing` (type T, field
    # data) would collapse to the same short name — the second, slot-free path
    # to the same crash.
    test_project.prepare(
        schema="""
        type Query {
            thing: T
        }

        type T {
            data: String
            other: String
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        f = api_gql("fragment TWith on T { other }")

        s = api_gql(
            '''
            query S {
                slotted: thing @slot { __typename }
            }
            '''
        )

        q = api_gql("query Q { thing { data } }")
        """,
    )
    assert test_project.generate() is True
    test_project.import_api()
