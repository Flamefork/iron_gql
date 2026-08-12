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
    reserved = CollectedUnionAlias(
        name="Foo",
        variants=(NamedRef(name="Alpha"), NamedRef(name="Beta")),
    )
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
        input_artifacts=[],
        operations=[],
        fragments=[],
        on_type_bases=[],
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


def test_short_name_collapse_yields_to_an_on_type_base_name(
    test_project: ProjectBuilder,
):
    # `fragment FragOnPost on Post` earns type `Post` an `OnPost` base (design's
    # §4/§6) once some template exists for `bind` to spread it into. A plain
    # query on a type literally named `OnPost` would otherwise collapse to
    # that same bare name (`_collapse_single_variant_types`) -- the base's
    # name has to be claimed as fixed for the collapse to yield instead of
    # generating two classes under one Python name.
    test_project.prepare(
        schema="""
        type Query {
            post: Post
            onPost: OnPost
        }

        type Post {
            id: ID!
        }

        type OnPost {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        frag = api_gql("fragment FragOnPost on Post { id }")
        tmpl = api_gql("query Tmpl { post @slot { __typename } }")
        q = api_gql("query QOnPost { onPost { id } }")
        """,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class OnPostWithId(" in generated
    assert "class OnPost(slots.GQLBindableFragment[TModel, TReads], ABC):" in generated


def test_fragment_named_like_a_base_is_diagnosed(test_project: ProjectBuilder):
    # `fragment FragOnPost on Post` earns `Post` an `OnPost` base; a second,
    # unrelated fragment spelled `OnPost` (on a different type) claims the
    # exact same Python name for its own class -- an ordinary two-fixed-names
    # collision, not a rename-map one, so it must be diagnosed even though
    # neither side is ever renamed.
    test_project.prepare(
        schema="""
        type Query {
            post: Post
            bar: Bar
        }

        type Post {
            id: ID!
        }

        type Bar {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        base_frag = api_gql("fragment FragOnPost on Post { id }")
        clashing_frag = api_gql("fragment OnPost on Bar { id }")
        tmpl = api_gql("query Tmpl { post @slot { __typename } }")
        """,
    )
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    message = str(exc_info.value)
    assert "OnPost" in message
    assert "on-type base" in message
    assert "fragment 'OnPost'" in message


def test_private_applied_name_does_not_force_public_model_rename(
    test_project: ProjectBuilder,
):
    # Factory `Foo` получает private applied class `_FooApplied`. Поэтому model
    # GraphQL type `FooApplied` сохраняет короткое публичное имя: два namespace
    # больше не конкурируют за `FooApplied`.
    test_project.prepare(
        schema="""
        type Query {
            bar: Bar
            fooApplied: FooApplied
        }

        type Bar {
            id(size: Int): ID!
        }

        type FooApplied {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        foo = api_gql("fragment Foo on Bar { id(size: $size) }")
        tmpl = api_gql("query Tmpl { bar @slot { __typename } }")
        q = api_gql("query QFooApplied { fooApplied { id } }")
        """,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class FooApplied(" in generated
    assert 'class _FooApplied(OnBar[FooData, "_FooApplied"]):' in generated


def test_fragment_named_like_another_slots_bind_type_param_is_diagnosed(
    test_project: ProjectBuilder,
):
    # `bind()`'s own PEP 695 type parameters (`TModel{Slot}`/`TReads{Slot}`,
    # render.slot_type_param_names) are a namespace `_signature_claims`
    # otherwise only checks slots against each other for -- a fragment never
    # entered that check. A fragment literally named `TModelPost` here is
    # spread-compatible with slot `attachment`, not `post`, and a `.bind(...)`
    # naming two fragments for `attachment` renders a literal-tuple overload
    # (`tuple[FragB, TModelPost] | ...`) that also fills `post` from its base
    # form, declaring a `TModelPost` type parameter of its own. PEP 695 scopes
    # a generic function's type parameters over its whole signature, so that
    # overload would silently read `TModelPost` in the tuple form as the
    # type variable instead of the fragment -- caught here rather than shipped
    # as a wrong annotation nothing flags.
    test_project.prepare(
        schema="""
        type Query {
            thing: Thing
        }

        type Thing {
            post: Post
            attachment: Attachment
        }

        type Post {
            id: ID!
        }

        union Attachment = A | B

        type A {
            id: ID!
        }

        type B {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        frag_post = api_gql("fragment FragOnPost on Post { id }")
        frag_a = api_gql("fragment TModelPost on A { id }")
        frag_b = api_gql("fragment FragB on B { id }")
        tmpl = api_gql(
            '''
            query GetThing {
                thing {
                    post @slot { __typename }
                    attachment @slot { __typename }
                }
            }
            '''
        )
        bound = tmpl.bind(post=frag_post, attachment=(frag_a, frag_b))
        """,
    )
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    message = str(exc_info.value)
    assert "'TModelPost'" in message
    assert "the type parameters of overload" in message
    assert "of template 'GetThing'" in message
    assert "the bind() type parameter of slot 'post'" in message
    assert "fragment 'TModelPost'" in message


def test_private_applied_factory_name_does_not_collide_with_bind_type_param(
    test_project: ProjectBuilder,
):
    # Slot `postApplied` создаёт type parameter `TModelPostApplied`, а factory
    # fragment `TModelPost` создаёт private application class
    # `_TModelPostApplied`. Разные имена
    # должны сосуществовать: private applied type больше не отнимает публичный
    # namespace у bind signature.
    test_project.prepare(
        schema="""
        type Query {
            thing: Thing
        }

        type Thing {
            post: Post
            tag: Tag
        }

        type Post {
            id: ID!
        }

        type Tag {
            value(x: Int): String!
            label: String!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        frag = api_gql("fragment TModelPost on Tag { value(x: $x) }")
        other = api_gql("fragment OtherTag on Tag { label }")
        post = api_gql("fragment PostParts on Post { id }")
        tmpl = api_gql(
            '''
            query GetThing {
                thing {
                    postApplied: post @slot { __typename }
                    tag @slot { __typename }
                }
            }
            '''
        )
        bound = tmpl.bind(tag=(frag.with_args(x=1), other))
        """,
    )
    assert test_project.generate() is True
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "_TModelPostApplied" in generated
    assert "TModelPostApplied" in generated


def test_fragment_named_like_its_own_slots_bind_type_param_is_accepted(
    test_project: ProjectBuilder,
):
    # The same coincidence as the sibling test above, but the fragment is
    # compatible with slot `post` alone -- the very slot whose type
    # parameters share its name -- not with any other slot. A single slot
    # picks exactly one form per overload (`render.bind_signatures`' Cartesian
    # product), so `post`'s own base form (declaring `TModelPost`) and a
    # tuple form naming this fragment for `post` itself never coexist in one
    # signature: nothing is actually shadowed, and this must generate.
    test_project.prepare(
        schema="""
        type Query {
            thing: Thing
        }

        type Thing {
            post: Post
            attachment: Attachment
        }

        type Post {
            id: ID!
        }

        union Attachment = A | B

        type A {
            id: ID!
        }

        type B {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        frag_post_a = api_gql("fragment TModelPost on Post { id }")
        frag_post_b = api_gql("fragment FragPostB on Post { id }")
        tmpl = api_gql(
            '''
            query GetThing {
                thing {
                    post @slot { __typename }
                    attachment @slot { __typename }
                }
            }
            '''
        )
        bound = tmpl.bind(post=(frag_post_a, frag_post_b))
        """,
    )
    assert test_project.generate() is True


TUPLE_FILL_SCHEMA = """
type Query {
    thing: Thing
}

type Thing {
    attachment: Attachment
}

union Attachment = A | B

type A {
    id: ID!
}

type B {
    id: ID!
}
"""


def _tuple_fill_queries(fragment: str) -> str:
    return f"""
    from sample_app.gql.api import api_gql

    frag_a = api_gql("fragment {fragment} on A {{ id }}")
    frag_b = api_gql("fragment FragB on B {{ id }}")
    tmpl = api_gql(
        '''
        query GetThing {{
            thing {{
                attachment @slot {{ __typename }}
            }}
        }}
        '''
    )
    bound = tmpl.bind(attachment=(frag_a, frag_b))
    """


def test_fragment_named_like_a_tuple_positions_type_param_is_diagnosed(
    test_project: ProjectBuilder,
):
    # A literal tuple form declares one constrained parameter per position
    # (`render.slot_tuple_param_names`) and spells the fragment classes inside
    # those constraints -- so a fragment named `TFillAttachment1` is read as
    # the position's own type variable there, and the constraint meant to name
    # it names nothing. The base form's twin of this is
    # `test_fragment_named_like_another_slots_bind_type_param_is_diagnosed`;
    # the two parameter sets are separate namespaces and a claim on one is no
    # claim on the other.
    test_project.prepare(
        schema=TUPLE_FILL_SCHEMA, queries=_tuple_fill_queries("TFillAttachment1")
    )
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    message = str(exc_info.value)
    assert "'TFillAttachment1'" in message
    assert "the type parameters of overload" in message
    assert "of template 'GetThing'" in message
    assert "position 1 of slot 'attachment'" in message
    assert "fragment 'TFillAttachment1'" in message


def test_fragment_named_past_the_tuples_own_arity_is_accepted(
    test_project: ProjectBuilder,
):
    # The claim is no wider than the forms: this slot's tuples are written
    # with two fragments, so there is no third position and no
    # `TFillAttachment3` for anything to collide with. A claim covering
    # positions no form declares would refuse a legal GraphQL name for
    # nothing.
    test_project.prepare(
        schema=TUPLE_FILL_SCHEMA, queries=_tuple_fill_queries("TFillAttachment3")
    )
    assert test_project.generate() is True


def test_tuple_overloads_of_different_arities_have_distinct_namespaces(
    test_project: ProjectBuilder,
):
    # Каждая arity рендерится отдельным overload. Поэтому повторное использование
    # positional type parameter names в arity-three легально:
    # TFillAttachment1/2 не делят namespace Python с overload arity-two.
    test_project.prepare(
        schema="""
        type Query {
            thing: Thing
        }

        type Thing {
            attachment: Attachment
        }

        union Attachment = A | B | C

        type A { id: ID! }
        type B { id: ID! }
        type C { id: ID! }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        frag_a = api_gql("fragment FragA on A { id }")
        frag_b = api_gql("fragment FragB on B { id }")
        frag_c = api_gql("fragment FragC on C { id }")
        tmpl = api_gql(
            '''
            query GetThing {
                thing {
                    attachment @slot { __typename }
                }
            }
            '''
        )
        pair = tmpl.bind(attachment=(frag_a, frag_b))
        triple = tmpl.bind(attachment=(frag_a, frag_b, frag_c))
        """,
    )
    _api, _queries = test_project.generate_and_import()
    generated_path = test_project.root / "sample_app/gql/api.py"
    generated = generated_path.read_text()
    assert "attachment: tuple[TFillAttachment1, TFillAttachment2]" in generated
    arity_three = (
        "attachment: tuple[TFillAttachment1, TFillAttachment2, TFillAttachment3]"
    )
    assert arity_three in generated


@pytest.mark.parametrize(
    ("template", "slot", "collision", "claimed_by"),
    [
        ("TModelX", "xResult", "TModelXResult", "the result class of template"),
        ("TReadsX", "xBound", "TReadsXBound", "the bound base of template"),
    ],
)
def test_a_slot_completing_the_templates_own_class_name_is_diagnosed(
    test_project: ProjectBuilder,
    template: str,
    slot: str,
    collision: str,
    claimed_by: str,
):
    # The two names every `bind()` overload spells whatever fills its slots:
    # the return type is `{Template}Bound[{Template}Result[...]]`
    # (`render._bind_signature`). Both are composed of the template's own
    # class name, so a slot whose Pascal spelling completes one of them makes
    # `render.slot_type_param_names` declare a type parameter of exactly that
    # name over a signature that reads it -- and PEP 695 scopes a generic
    # function's parameters over its whole signature, so the annotation means
    # the type variable. Generation passed and the module it wrote failed to
    # type-check with `TypeVar ... is not subscriptable`, which is the
    # contract `test_generated_typecheck` holds every package to.
    #
    # A template named after a type-parameter prefix rather than a plain name
    # because that is what it takes for the two halves to meet: the parameters
    # are spelled `TModel{Slot}`/`TReads{Slot}` and the classes
    # `{Template}Result`/`{Template}Bound`, so the collision needs a template
    # carrying one prefix and a slot completing the other suffix. Both
    # parameters and both classes, crossed: either parameter can land on
    # either class, and claiming one name is no claim on the other.
    test_project.prepare(
        schema=f"""
        type Query {{
            thing: Thing
        }}

        type Thing {{
            {slot}: Detail
        }}

        type Detail {{
            label: String!
        }}
        """,
        queries=f"""
        from sample_app.gql.api import api_gql

        detail_parts = api_gql("fragment DetailParts on Detail {{ label }}")
        tmpl = api_gql(
            '''
            query {template} {{
                thing {{
                    {slot} @slot {{ __typename }}
                }}
            }}
            '''
        )
        """,
    )
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    message = str(exc_info.value)
    assert f"'{collision}'" in message
    assert "the type parameters of overload" in message
    assert f"of template '{template}'" in message
    assert f"{claimed_by} '{template}'" in message


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
