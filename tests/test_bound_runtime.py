from collections.abc import Mapping
from typing import Any
from typing import ClassVar

import pydantic
import pytest

from iron_gql import runtime
from iron_gql import slots


class _Data(pydantic.BaseModel):
    url: str


class _ImageDefinition(slots.GQLFragment[_Data, Any]):
    _adapter__: ClassVar[pydantic.TypeAdapter[_Data]] = pydantic.TypeAdapter(_Data)

    def __init__(self) -> None:
        super().__init__(
            fragment_name="Image",
            definition_type=_ImageDefinition,
            adapter=self._adapter__,
        )


class _AltDefinition(slots.GQLFragment[_Data, Any]):
    _adapter__: ClassVar[pydantic.TypeAdapter[_Data]] = pydantic.TypeAdapter(_Data)

    def __init__(self) -> None:
        super().__init__(
            fragment_name="Alt",
            definition_type=_AltDefinition,
            adapter=self._adapter__,
        )


class _Application(slots.GQLBindableFragment[_Data, Any]):
    def __init__(
        self,
        definition_type: type[slots.GQLFragment[_Data, Any]],
        name: str,
        args: dict[str, object],
    ) -> None:
        super().__init__(
            fragment_name=name,
            definition_type=definition_type,
            adapter=pydantic.TypeAdapter(_Data),
        )
        self._set_fragment_args(args)


class _ImageApplication(_Application):
    pass


class _AltApplication(_Application):
    pass


def _application(
    definition_type: type[slots.GQLFragment[_Data, Any]],
    name: str,
    args: dict[str, object] | None = None,
) -> slots.GQLBindableFragment[_Data, Any]:
    return _Application(definition_type, name, args if args is not None else {})


def test_with_headers_preserves_fragment_args():
    # `fragment_args` один раз задаётся в `bound__` из значений applied
    # fragment; на уровне bound нет `with_args`, который мог бы его заменить.
    # `with_headers` копирует объект через `_copy`, поэтому тот обязан переносить
    # значения вместе с `exec_source` и `slot_readers`.
    spec: runtime.BoundSpec = (
        "query Q { x }",
        {"x": ((_ImageDefinition, frozenset()),)},
    )
    fragment = _application(_ImageDefinition, "Image", args={"limit": 1})
    bound = runtime.GQLBoundOperation.bound__(spec, {"x": (fragment,)})
    op = bound.with_headers({"A": "b"})
    assert op.headers == {"A": "b"}
    assert op.fragment_args == {"limit": 1}


def test_operation_object_protocol_does_not_expose_request_state():
    token = "Bearer secret-token"
    fragment_secret = "secret-fragment-value"
    operation = runtime.GQLOperation().with_headers({"Authorization": token})
    spec: runtime.BoundSpec = (
        "query Q { x }",
        {"x": ((_ImageDefinition, frozenset()),)},
    )
    fragment = _application(_ImageDefinition, "Image", args={"value": fragment_secret})
    bound = runtime.GQLBoundOperation.bound__(spec, {"x": (fragment,)}).with_headers({
        "Authorization": token
    })
    equivalent_bound = runtime.GQLBoundOperation.bound__(
        spec, {"x": (fragment,)}
    ).with_headers({"Authorization": token})

    assert token not in repr(operation)
    assert token not in repr(bound)
    assert fragment_secret not in repr(bound)
    assert runtime.GQLOperation() != runtime.GQLOperation()
    assert bound != equivalent_bound


def test_bound_constructs_readers_from_definition_classes():
    first = _application(_ImageDefinition, "Image")
    second = _application(_ImageDefinition, "Image")
    spec: runtime.BoundSpec = (
        "query Q { x y }",
        {
            "x": ((_ImageDefinition, frozenset({"T"})),),
            "y": ((_ImageDefinition, frozenset({"T"})),),
        },
    )
    bound = runtime.GQLBoundOperation.bound__(spec, {"x": (first,), "y": (second,)})
    [x_reader] = bound.slot_readers["x"]
    [y_reader] = bound.slot_readers["y"]
    assert type(x_reader.definition) is _ImageDefinition
    assert type(y_reader.definition) is _ImageDefinition
    assert x_reader.definition is not y_reader.definition


def test_bound_merges_fragment_args_flat_across_slots():
    # `GQLBoundOperation.bound__`'s own comment claims this: two different
    # fragments' values merge into one flat dict as long as their variable
    # names do not collide. Non-colliding is the only shape `bound__` ever
    # sees for two *directly*-named fragments -- a collision between them is
    # rejected at generation (`bindings._direct_fragment_variable_
    # collisions`) -- so this is the positive case the comment leans on.
    left = _application(_ImageDefinition, "Image", args={"width": 100})
    right = _application(_AltDefinition, "Alt", args={"height": 50})
    spec: runtime.BoundSpec = (
        "query Q { x y }",
        {
            "x": ((_ImageDefinition, frozenset()),),
            "y": ((_AltDefinition, frozenset()),),
        },
    )
    bound = runtime.GQLBoundOperation.bound__(spec, {"x": (left,), "y": (right,)})
    assert bound.fragment_args == {"width": 100, "height": 50}


def test_bound_tells_two_uploads_apart_though_their_json_is_the_same():
    # The direction the wire comparison must not lose. A `FileVar` serializes
    # to `null` with the bytes riding in the multipart body instead
    # (`serialize_variables`), so two different uploads have identical JSON
    # and are one request only if they are the same file -- which is why
    # `_request_shape` carries the file identities beside the JSON. Spelled
    # against `bound__` directly: an `Upload` fragment variable reaches this
    # merge the same way any other does, and this is the shortest application that
    # carries one.
    one, two = runtime.FileVar(b"one"), runtime.FileVar(b"two")
    spec: runtime.BoundSpec = (
        "query Q { x y }",
        {
            "x": ((_ImageDefinition, frozenset()),),
            "y": ((_ImageDefinition, frozenset()),),
        },
    )

    def bind(first: runtime.FileVar, second: runtime.FileVar) -> Mapping[str, object]:
        bound = runtime.GQLBoundOperation.bound__(
            spec,
            {
                "x": (_application(_ImageDefinition, "Image", args={"file": first}),),
                "y": (_application(_ImageDefinition, "Image", args={"file": second}),),
            },
        )
        return bound.fragment_args

    with pytest.raises(ValueError, match=r"conflicting values.*\$file"):
        _ = bind(one, two)
    # The same object twice is one upload and one request.
    assert bind(one, one)["file"] is one


def test_binding_key_sorts_slots_and_fragment_classes():
    a = _ImageApplication(_ImageDefinition, "Image", {})
    b = _AltApplication(_AltDefinition, "Alt", {})
    key = slots.binding_key({"s2": b, "s1": [b, a]})
    assert key == (
        ("s1", (_AltApplication, _ImageApplication)),
        ("s2", (_AltApplication,)),
    )


def test_binding_key_omitted_slot_and_explicit_empty_list_agree():
    # Codegen's binding specs are built from the discovered `.bind(...)`
    # call, which only ever has the omitted-slot shape — so a caller who
    # spells "no fragments for this slot" as `slot=[]` instead of leaving the
    # kwarg out entirely must still land on the same binding entry.
    a = _ImageApplication(_ImageDefinition, "Image", {})
    omitted = slots.binding_key({"s1": a})
    explicit_empty = slots.binding_key({"s1": a, "s2": []})
    assert omitted == explicit_empty
    assert omitted == (("s1", (_ImageApplication,)),)
