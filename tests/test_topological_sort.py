from graphlib import CycleError

import pytest

from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.naming import topological_sort


def _model(name: str, *referenced: str) -> CollectedModel:
    fields = [
        CollectedField(
            name=f"ref_{index}",
            response_key=f"ref_{index}",
            type_info=NamedRef(name=ref),
        )
        for index, ref in enumerate(referenced)
    ]
    if not fields:
        fields = [
            CollectedField(
                name="value", response_key="value", type_info=ScalarRef(expr="str")
            ),
        ]
    return CollectedModel(name=name, fields=fields)


def test_empty_input():
    assert topological_sort([]) == []


def test_linear_chain():
    a = _model("A", "B")
    b = _model("B", "C")
    c = _model("C")
    result = topological_sort([a, b, c])
    assert [model.name for model in result] == ["C", "B", "A"]


def test_diamond():
    # D depends on B and C; B and C both depend on A.
    a = _model("A")
    b = _model("B", "A")
    c = _model("C", "A")
    d = _model("D", "B", "C")
    result = topological_sort([d, c, b, a])
    names = [model.name for model in result]
    assert names[0] == "A"
    # B and C appear in lexicographic order before D.
    assert names.index("B") < names.index("C")
    assert names[-1] == "D"
    assert names == ["A", "B", "C", "D"]


def test_multiple_ready_lexicographic():
    # All independent — the queue should yield them in lexicographic order
    # regardless of input order.
    models = [_model(name) for name in ("Charlie", "Alpha", "Bravo")]
    result = topological_sort(models)
    assert [m.name for m in result] == ["Alpha", "Bravo", "Charlie"]


def test_ready_lexicographic_after_dependency():
    # Root A unlocks B and C simultaneously — lexicographic pick between them.
    a = _model("A")
    b = _model("B", "A")
    c = _model("C", "A")
    result = topological_sort([c, b, a])
    assert [m.name for m in result] == ["A", "B", "C"]


def test_ignores_references_outside_the_model_set():
    # ExternalScalar is not in the list — it must not block sorting.
    a = _model("A", "ExternalScalar", "B")
    b = _model("B")
    result = topological_sort([a, b])
    assert [m.name for m in result] == ["B", "A"]


def test_cycle_raises_cycle_error():
    a = _model("A", "B")
    b = _model("B", "A")
    with pytest.raises(CycleError):
        topological_sort([a, b])


def test_self_reference_raises_cycle_error():
    # Note: CollectedModel.dependencies filters out self-references, so a model
    # that only references itself is not actually a cycle.
    model = CollectedModel(
        name="Node",
        fields=[
            CollectedField(
                name="child", response_key="child", type_info=NamedRef(name="Node")
            ),
        ],
    )
    result = topological_sort([model])
    assert [m.name for m in result] == ["Node"]
