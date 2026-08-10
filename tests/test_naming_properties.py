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
as markers -- two different shapes must never end up under one name.
"""

from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import GraphQLGenerationError
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


def _model(index: int, fields: list[CollectedField]) -> CollectedModel:
    return CollectedModel(name=f"Raw{index}", graphql_type_name="T", fields=fields)


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
    ir = CollectedPackageIR(
        result_artifacts=[first, second],
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
    # The whole contract, not half of it: the encoder is allowed to run out of
    # distinguishable names -- it concatenates tokens, and a schema is free to
    # spell one of them -- but then generation must *say so*. What it may
    # never do is hand two shapes one class.
    try:
        renamed = apply_rename(ir, frozenset())
    except GraphQLGenerationError:
        return
    names = [artifact.name for artifact in renamed.result_artifacts]
    assert len(set(names)) == len(names), f"two shapes share a name: {names}"
