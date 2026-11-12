"""The naming phase, asked a question no example can answer for a family.

A generated model's name encodes its shape: the GraphQL type, the field names,
and the tokens of each field's rendered type. If that encoding is not
injective, two different shapes ask for one class -- and every instance of it
looks like a separate bug. Three families were found by hand this way
(`Opt` merging into a type named `OptFoo`; `{a_b, c}` and `{a, b_c}` both
spelling `ABC`; `[X]` spelling what `ListX` spells), each fixed one at a time
while the family stayed open.

So the question is asked of the family: over shapes drawn from an alphabet
built to collide -- underscores, casing, and the very words the encoder uses
as markers -- two different shapes must never end up under one name, and
generation must not give up on them either: a spelling two shapes share is
told apart by a suffix from each shape's own key.

ADR 0001 adds the second question: a name may vanish when another query
arrives, but it never moves to a different shape.
"""

from hypothesis import example
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedModuleIR
from iron_gql.codegen.ir import ListRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.naming import apply_rename

# Words the encoder itself emits (`List`, `Opt`) and separators it strips
# (`_`) are in the alphabet on purpose: a name is only ambiguous where a token
# the encoder writes can also be spelled by the schema.
FIELD_NAMES = st.sampled_from(["a", "b", "ab", "a_b", "aB", "b_c", "c"])

# A GraphQL schema gives every type one name, so a model and a scalar can
# never share one: drawing both from a single alphabet would report a
# collision no schema can produce. The traps stay -- they are just split.
OBJECT_NAMES = st.sampled_from(["X", "ListX", "OptFoo", "Foo", "A_B", "AB"])
SCALAR_NAMES = st.sampled_from(["String", "Int", "List", "Opt", "Date_Time"])


def _named(name: str, nullable: bool) -> TypeRef:
    return NamedRef(name=name, nullable=nullable)


def _scalar(hint: str | None, nullable: bool) -> TypeRef:
    return ScalarRef(expr="str", name_hint=hint, nullable=nullable)


def _listed(element: TypeRef, nullable: bool) -> TypeRef:
    return ListRef(element=element, nullable=nullable)


def _wrap(inner: st.SearchStrategy[TypeRef]) -> st.SearchStrategy[TypeRef]:
    return st.builds(_listed, inner, st.booleans())


def _types() -> st.SearchStrategy[TypeRef]:
    leaves = st.one_of(
        st.builds(_named, OBJECT_NAMES, st.booleans()),
        st.builds(_scalar, st.one_of(SCALAR_NAMES, st.none()), st.booleans()),
    )
    return st.recursive(leaves, _wrap, max_leaves=3)


def _field(name: str, typ: TypeRef) -> CollectedField:
    return CollectedField(
        name=name, response_key=name, type_info=typ, is_conditional=False
    )


def _fields() -> st.SearchStrategy[list[CollectedField]]:
    return st.lists(
        st.builds(_field, FIELD_NAMES, _types()),
        min_size=1,
        max_size=3,
        unique_by=lambda field: field.name,
    )


def _model(
    index: int, fields: list[CollectedField], gql_type: str = "T"
) -> CollectedModel:
    return CollectedModel(name=f"Raw{index}", graphql_type_name=gql_type, fields=fields)


def _module(models: list[CollectedModel]) -> CollectedModuleIR:
    return CollectedModuleIR(
        result_artifacts=list(models),
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


type Shape = tuple[str | None, tuple[CollectedField, ...]]


def _shapes_by_name(models: list[CollectedModel]) -> dict[str, Shape]:
    renamed = apply_rename(_module(models), frozenset()).result_artifacts
    return {
        model.name: (model.graphql_type_name, model.shape_key)
        for model in renamed
        if isinstance(model, CollectedModel)
    }


@settings(max_examples=400, deadline=None)
@given(_fields(), _fields())
def test_two_shapes_are_never_named_alike(
    left: list[CollectedField], right: list[CollectedField]
):
    first, second = _model(1, left), _model(2, right)
    if first.shape_key == second.shape_key:
        # Identical shapes are *meant* to converge -- that is what lets two
        # selections of one type share a class.
        return
    names = list(_shapes_by_name([first, second]))
    assert len(names) == 2, f"two shapes share a name: {names}"


# `TWithA_String` is a legal GraphQL type name that spells the detailed name
# of a `T` shape: a bare name and a detailed one can meet.
GRAPHQL_TYPES = st.sampled_from(["T", "U", "TWithA_String"])


def _crowded_fields() -> st.SearchStrategy[list[CollectedField]]:
    # Few types, so that shapes keep meeting on one spelling: a name can only
    # move where a newcomer contests it.
    types = st.builds(_scalar, st.just("String"), st.booleans())
    return st.lists(
        st.builds(_field, FIELD_NAMES, types),
        min_size=1,
        max_size=3,
        unique_by=lambda field: field.name,
    )


def _string(name: str, nullable: bool) -> CollectedField:
    return _field(name, _scalar("String", nullable))


@settings(max_examples=400, deadline=None)
@example(
    # `{a_b, c}` and `{a, b_c}` spell one detailed name; with a second shape
    # in each slot neither collapses, so a newcomer could only take the
    # name `TWithABC_StringString` from its holder by winning a contest.
    [
        ("T", [_string("a_b", False), _string("c", False)]),
        ("T", [_string("a_b", True), _string("c", False)]),
        ("T", [_string("a", True), _string("b_c", False)]),
    ],
    ("T", [_string("a", False), _string("b_c", False)]),
)
@example(
    # A type spelled like another type's detailed name: the bare name
    # `TWithA_String` it holds must not pass to the `T` shape that spells it
    # once `T` has two shapes.
    [("TWithA_String", [_string("a", False)]), ("T", [_string("a", True)])],
    ("T", [_string("a", False)]),
)
@given(
    st.lists(st.tuples(GRAPHQL_TYPES, _crowded_fields()), min_size=1, max_size=4),
    st.tuples(GRAPHQL_TYPES, _crowded_fields()),
)
def test_another_query_never_moves_a_name_to_another_shape(
    existing: list[tuple[str, list[CollectedField]]],
    added: tuple[str, list[CollectedField]],
):
    models = [
        _model(index, fields, gql_type)
        for index, (gql_type, fields) in enumerate(existing)
    ]
    before = _shapes_by_name(models)
    after = _shapes_by_name([*models, _model(len(models), added[1], added[0])])
    for name, shape in before.items():
        assert after.get(name, shape) == shape, f"{name!r} moved to another shape"
