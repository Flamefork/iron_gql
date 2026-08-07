from typing import ClassVar

import pydantic
import pytest

from iron_gql import runtime
from iron_gql import slots


class _Data(pydantic.BaseModel):
    url: str


def _handle[TModel: pydantic.BaseModel](
    name: str, model: type[TModel] = _Data
) -> slots.GQLFragment[TModel]:
    return slots.GQLFragment(
        fragment_name=name,
        adapter=pydantic.TypeAdapter(model),
    )


class _Bound(runtime.GQLBoundOperation):
    exec_source__ = "query Q { x }"
    slot_handles__: ClassVar[slots.SlotHandles] = {}
    required_arg_names__ = frozenset({"limit"})


def test_with_args_returns_a_fresh_instance_carrying_exactly_the_given_args():
    op = _Bound()
    op2 = op.with_args__({"limit": 10, "offset": 1})
    op3 = op2.with_args__({"limit": 20})
    # Each call returns a fresh instance...
    assert op2 is not op
    assert op3 is not op2
    # ...carrying exactly what it was handed. Merging instead would make the
    # generated `with_args`'s own spelling of "use the schema default" — the
    # key left out of the mapping — unreachable after any call that set it.
    assert op3.fragment_args__() == {"limit": 20}


def test_missing_required_args_raise_with_names():
    with pytest.raises(ValueError, match=r"\$limit"):
        _Bound().fragment_args__()


def test_with_headers_preserves_fragment_args():
    op = _Bound().with_args__({"limit": 1}).with_headers({"A": "b"})
    assert op.headers == {"A": "b"}
    assert op.fragment_args__() == {"limit": 1}


def test_bind_key_sorts_slots_and_fragment_names():
    a = _handle("A")
    b = _handle("B")
    key = slots.bind_key("Tmpl", {"s2": b, "s1": [b, a]})
    assert key == ("Tmpl", (("s1", ("A", "B")), ("s2", ("B",))))


def test_bind_key_omitted_slot_and_explicit_empty_list_agree():
    # Codegen's dispatch table is built from the discovered `.bind(...)`
    # call, which only ever has the omitted-slot shape — so a caller who
    # spells "no fragments for this slot" as `slot=[]` instead of leaving the
    # kwarg out entirely must still land on the same dispatch entry.
    a = _handle("A")
    omitted = slots.bind_key("Tmpl", {"s1": a})
    explicit_empty = slots.bind_key("Tmpl", {"s1": a, "s2": []})
    assert omitted == explicit_empty
    assert omitted == ("Tmpl", (("s1", ("A",)),))
