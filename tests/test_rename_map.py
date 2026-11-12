import dataclasses
import hashlib
from collections.abc import Callable

import pytest

from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedModuleIR
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
    rename = build_rename_map([foo], frozenset(), {})
    assert rename == {"Foo_1": "Foo"}


def test_same_shape_deduplicates():
    # Two models with identical shape_key collapse onto the same candidate.
    shape_fields = [_field("a", _scalar("str")), _field("b", _scalar("int"))]
    first = CollectedModel(name="Foo_1", graphql_type_name="Foo", fields=shape_fields)
    second = CollectedModel(
        name="Foo_2", graphql_type_name="Foo", fields=list(shape_fields)
    )
    rename = build_rename_map([first, second], frozenset(), {})
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
    rename = build_rename_map([first, second], frozenset(), {})
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
    rename = build_rename_map([first, second, third], frozenset(), {})
    assert rename["Foo_1"] == "FooWithX_Alpha"
    assert rename["Foo_2"] == "FooWithX_Beta"
    assert rename["Foo_3"] == "FooWithX_Gamma"


def test_single_form_blocked_by_reserved_name_keeps_short_name():
    # A scaffold name already occupies the desired GraphQL type name: the
    # shape skips it and keeps its short form.
    foo = CollectedModel(
        name="Foo_1",
        graphql_type_name="Foo",
        fields=[_field("a", _scalar("str"))],
    )
    rename = build_rename_map([foo], frozenset({"Foo"}), {})
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
    rename = build_rename_map([foo, bar], frozenset(), {})
    assert rename["Foo_1"] == "Foo"
    assert rename["Bar_1"] == "Bar"


def _pkg(artifacts: list[CollectedModel | CollectedUnionAlias]) -> CollectedModuleIR:
    return CollectedModuleIR(
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
    first = build_rename_map([a, b], frozenset(), {})
    second = build_rename_map([b, a], frozenset(), {})
    assert first == second


def _underscore_twins() -> list[CollectedArtifact]:
    # `{a_b, c}` and `{a, b_c}` of one type: both spell the detailed
    # candidate below, so both are suffixed.
    nullable = ScalarRef(expr="str", name_hint="String", nullable=True)
    return [
        CollectedModel(
            name=f"T_{index}",
            graphql_type_name="T",
            fields=[_field(name, nullable) for name in names],
        )
        for index, names in enumerate([("a_b", "c"), ("a", "b_c")])
    ]


TWINS_CANDIDATE = "TWithABC_Opt_StringOpt_String"


def _rename_twins(reserved: frozenset[str]) -> dict[str, str]:
    twins = _underscore_twins()
    names = build_rename_map(twins, reserved, {})
    assert names == build_rename_map(twins[::-1], reserved, {})
    return names


@dataclasses.dataclass(frozen=True)
class _StandInHash:
    digest: str

    def hexdigest(self) -> str:
        return self.digest


def _stand_in_sha256(*, first: str, second: str) -> Callable[[bytes], _StandInHash]:
    # Replaces SHA-256 itself: the twins are told apart by their first field.
    def sha256(data: bytes) -> _StandInHash:
        return _StandInHash(first if b'"a_b"' in data else second)

    return sha256


def test_shared_digest_prefix_lengthens_every_claimant(monkeypatch: pytest.MonkeyPatch):
    # A contested name is suffixed with a prefix of the shape's own digest.
    # When two shapes share that prefix, neither keeps it -- both move to a
    # longer one -- and the answer does not depend on input order.
    sha256 = _stand_in_sha256(first="0" * 8 + "1" * 56, second="0" * 8 + "2" * 56)
    monkeypatch.setattr(hashlib, "sha256", sha256)
    assert set(_rename_twins(frozenset()).values()) == {
        f"{TWINS_CANDIDATE}_{'0' * 8}{'1' * 8}",
        f"{TWINS_CANDIDATE}_{'0' * 8}{'2' * 8}",
    }


def test_suffix_taken_by_a_fixed_name_lengthens():
    names = _rename_twins(frozenset())
    assert all(len(name) == len(TWINS_CANDIDATE) + 9 for name in names.values())
    taken = names["T_0"]
    lengthened = _rename_twins(frozenset({taken}))
    assert lengthened["T_1"] == names["T_1"]
    assert lengthened["T_0"].startswith(taken)
    assert len(lengthened["T_0"]) == len(taken) + 8


def _taken_suffixes(count: int) -> frozenset[str]:
    # The first `count` names of twin `T_0`, each taken as a fixed name.
    taken: frozenset[str] = frozenset()
    for _ in range(count):
        taken |= {_rename_twins(taken)["T_0"]}
    return taken


def test_every_name_of_a_shape_taken_is_diagnosed():
    # Fixed names are valid input -- an operation may be named anything. A
    # shape whose every suffix is taken gets no name rather than a count past
    # them, which a name spelled like the count could later take over.
    taken = _taken_suffixes(4)
    assert max(len(name) for name in taken) == len(TWINS_CANDIDATE) + 65
    with pytest.raises(GraphQLGenerationError) as exc_info:
        _ = _rename_twins(taken)
    [error] = exc_info.value.errors
    assert error.startswith(
        f"No name is left for the selection named '{TWINS_CANDIDATE}'"
    )
    assert "(taken)" in error
    assert "rename the operation, fragment or GraphQL type" in error


def test_type_spelled_like_the_last_suffix_is_diagnosed():
    # A GraphQL type spelled like a shape's full-digest name contests it, so
    # the shape cannot take it either.
    taken = _taken_suffixes(3)
    full = _rename_twins(taken)["T_0"]
    spelled_alike = CollectedModel(
        name="Raw",
        graphql_type_name=full,
        fields=[_field("id", _scalar("str"))],
    )
    with pytest.raises(GraphQLGenerationError, match="also spelled elsewhere"):
        _ = build_rename_map([*_underscore_twins(), spelled_alike], taken, {})


def test_shapes_with_one_full_digest_are_diagnosed(monkeypatch: pytest.MonkeyPatch):
    # Equal over the full digest, two shapes have no suffix to tell them
    # apart, and neither is handed a name by order.
    sha256 = _stand_in_sha256(first="0" * 64, second="0" * 64)
    monkeypatch.setattr(hashlib, "sha256", sha256)
    with pytest.raises(GraphQLGenerationError) as exc_info:
        _ = _rename_twins(frozenset())
    assert len(exc_info.value.errors) == 2


def test_fragment_named_like_a_base_is_diagnosed(test_project: ProjectBuilder):
    # `fragment FragOnPost on PostFragment` earns `PostFragment` an
    # `OnPostFragment` base; a second, unrelated fragment spelled `OnPost` (on
    # a different type) claims the exact same Python name for its own class
    # -- an ordinary two-fixed-names collision, not a rename-map one, so it
    # must be diagnosed even though neither side is ever renamed.
    test_project.prepare(
        schema="""
        type Query {
            post: PostFragment
            bar: Bar
        }

        type PostFragment {
            id: ID!
        }

        type Bar {
            id: ID!
        }
        """,
        queries="""
        from sample_app.gql.api import api_gql

        base_frag = api_gql("fragment FragOnPost on PostFragment { id }")
        clashing_frag = api_gql("fragment OnPost on Bar { id }")
        tmpl = api_gql("query Tmpl { post @slot { __typename } }")
        """,
    )
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    message = str(exc_info.value)
    assert "OnPostFragment" in message
    assert "on-type base" in message
    assert "fragment 'OnPost'" in message


def test_private_applied_name_does_not_force_public_model_rename(
    test_project: ProjectBuilder,
):
    # Factory `Foo` gets the private applied class `_FooFragmentApplied`. So the
    # model of GraphQL type `FooFragmentApplied` keeps its short public name:
    # the two namespaces no longer compete for `FooFragmentApplied`.
    test_project.prepare(
        schema="""
        type Query {
            bar: Bar
            fooApplied: FooFragmentApplied
        }

        type Bar {
            id(size: Int): ID!
        }

        type FooFragmentApplied {
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
    test_project.generate()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "class FooFragmentApplied(" in generated
    assert (
        'class _FooFragmentApplied(OnBar[FooData, "_FooFragmentApplied"]):' in generated
    )


def test_fragment_named_like_another_slots_bind_type_param_is_diagnosed(
    test_project: ProjectBuilder,
):
    # `bind()`'s own PEP 695 type parameters (`TModel{Slot}`/`TReads{Slot}`,
    # render.slot_type_param_names) are a namespace `_signature_claims`
    # otherwise only checks slots against each other for -- a fragment never
    # entered that check. A fragment named `TModelPost` -- its class
    # `TModelPostFragment` -- is spread-compatible with slot `attachment`, not
    # `postFragment`, and a `.bind(...)` naming two fragments for `attachment`
    # renders a literal-tuple overload (`tuple[FragBFragment,
    # TModelPostFragment] | ...`) that also fills `postFragment` from its base
    # form, declaring a `TModelPostFragment` type parameter of its own. PEP
    # 695 scopes a generic function's type parameters over its whole
    # signature, so that overload would silently read `TModelPostFragment` in
    # the tuple form as the type variable instead of the fragment -- caught
    # here rather than shipped as a wrong annotation nothing flags.
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
                    postFragment: post @slot { __typename }
                    attachment @slot { __typename }
                }
            }
            '''
        )
        bound = tmpl.bind(post_fragment=frag_post, attachment=(frag_a, frag_b))
        """,
    )
    with pytest.raises(GraphQLGenerationError) as exc_info:
        test_project.generate()
    message = str(exc_info.value)
    assert "'TModelPostFragment'" in message
    assert "the type parameters of overload" in message
    assert "of template 'GetThingQuery'" in message
    assert "the bind() type parameter of slot 'postFragment'" in message
    assert "fragment 'TModelPost'" in message


def test_private_applied_factory_name_does_not_collide_with_bind_type_param(
    test_project: ProjectBuilder,
):
    # Slot `postFragmentApplied` creates the type parameter
    # `TModelPostFragmentApplied`, and the factory fragment `TModelPost` creates
    # the private application class `_TModelPostFragmentApplied`. The different
    # names must coexist: the private applied type no longer takes the public
    # namespace away from the bind signature.
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
                    postFragmentApplied: post @slot { __typename }
                    tag @slot { __typename }
                }
            }
            '''
        )
        bound = tmpl.bind(tag=(frag.with_args(x=1), other))
        """,
    )
    test_project.generate()
    generated = (test_project.root / "sample_app/gql/api.py").read_text()
    assert "_TModelPostFragmentApplied" in generated
    assert "TModelPostFragmentApplied" in generated


def test_fragment_named_like_its_own_slots_bind_type_param_is_accepted(
    test_project: ProjectBuilder,
):
    # The same coincidence as the sibling test above, but the fragment is
    # compatible with slot `postFragment` alone -- the very slot whose type
    # parameters share its class name -- not with any other slot. A single
    # slot picks exactly one form per overload (`render.bind_signatures`'
    # Cartesian product), so the slot's own base form (declaring
    # `TModelPostFragment`) and a tuple form naming this fragment for the
    # slot itself never coexist in one
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
                    postFragment: post @slot { __typename }
                    attachment @slot { __typename }
                }
            }
            '''
        )
        bound = tmpl.bind(post_fragment=(frag_post_a, frag_post_b))
        """,
    )
    test_project.generate()


def test_tuple_overloads_of_different_arities_have_distinct_namespaces(
    test_project: ProjectBuilder,
):
    # Each arity is rendered as a separate overload. So reusing positional type
    # parameter names in arity-three is legal: TFillAttachment1/2 do not share a
    # Python namespace with the arity-two overload.
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
        (
            "TModelX",
            "xQueryResult",
            "TModelXQueryResult",
            "the result class of template",
        ),
        ("TReadsX", "xQueryBound", "TReadsXQueryBound", "the bound base of template"),
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
    # contract `test_generated_typecheck` holds every module to.
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
    assert f"of template '{template}Query'" in message
    assert f"{claimed_by} '{template}'" in message
