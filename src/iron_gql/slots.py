from collections.abc import Callable
from collections.abc import Sequence
from typing import ClassVar
from typing import Self
from typing import TypeIs
from typing import cast

import pydantic

# The raw JSON object under a slot's response key. Mirrors the `_Walkable`
# pattern in runtime.py: TypeIs narrowers from `object` keep basedpyright's
# `all` mode from leaking Unknown into call sites.
type SlotPayload = dict[str, object]


def _is_payload(value: object) -> TypeIs[SlotPayload]:
    return isinstance(value, dict)


def _is_typename(value: object) -> TypeIs[str]:
    return isinstance(value, str)


type SlotFragments = dict[str, tuple[GQLFragment[pydantic.BaseModel], ...]]


class GQLFragment[TModel: pydantic.BaseModel]:
    # Metadata lives on the instance, taken as required constructor
    # arguments: `TypeAdapter` is invariant in its parameter, so an adapter
    # that does not match `TModel` is a static error at the generated
    # `super().__init__` call, and a subclass that forgets its metadata
    # cannot even be instantiated — neither invariant needs a runtime check.
    def __init__(
        self,
        *,
        fragment_name: str,
        fragment_def: str,
        covered_typenames: frozenset[str],
        adapter: pydantic.TypeAdapter[TModel],
    ) -> None:
        self.fragment_name__ = fragment_name
        self.fragment_def__ = fragment_def
        # The snapshot of runtime typenames the fragment's own selection
        # covers: a payload of any other type reads back as None.
        self.covered_typenames__ = covered_typenames
        # Only the bound validate function is stored: a `TypeAdapter[TModel]`
        # attribute would put TModel in an invariant position and destroy the
        # inferred covariance that lets shared code accept
        # `SomeFragment[pydantic.BaseModel]`; in return position TModel keeps
        # the class covariant and `validate__` fully typed.
        self._validate: Callable[[object], TModel] = adapter.validate_python

    def validate__(self, payload: object) -> TModel:
        return self._validate(payload)

    # A handle is an identity token: `read` finds its data by the reader's
    # own reference, so copying machinery (deepcopy of a validated node,
    # model_copy(deep=True)) must never clone a stored handle out from under
    # the module-level singleton doing the reading.
    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object] | None) -> Self:
        return self

    def read(self, node: "GQLSlotNode | None") -> TModel | None:
        if node is None:
            return None
        return node.slot_data__(self)


def _is_handles(
    value: object,
) -> TypeIs[tuple[GQLFragment[pydantic.BaseModel], ...]]:
    if not isinstance(value, tuple):
        return False
    handles = cast("tuple[object, ...]", value)
    return all(isinstance(handle, GQLFragment) for handle in handles)


class GQLSlotNode(pydantic.BaseModel):
    slot_name__: ClassVar[str]

    # Keyed by the handle's id() with the handle itself held in the value:
    # the strong reference keeps every offered handle alive, so a recycled
    # address can never collide with a stored key — a wiring bug must not
    # look like a legitimate mismatch. Identity by id(), not by dict key
    # lookup, so a subclass overriding __eq__/__hash__ cannot alias one
    # fragment's data to another, and the entries survive deepcopy and
    # in-process pickling of a validated node.
    _slot_data: dict[int, tuple[GQLFragment[pydantic.BaseModel], object]] = (
        pydantic.PrivateAttr(default_factory=dict)
    )

    def slot_data__[TData: pydantic.BaseModel](
        self, handle: GQLFragment[TData]
    ) -> TData | None:
        # Eager validation writes an entry for every handle offered to the
        # slot — the validated model on a typename match, None otherwise — so
        # a missing key means the handle was never offered at all, which is a
        # wiring bug and must not be mistaken for a typename mismatch.
        entry = self._slot_data.get(id(handle))
        if entry is None:
            msg = (
                f"fragment '{handle.fragment_name__}' was not passed to "
                f"slot '{self.slot_name__}'"
            )
            raise ValueError(msg)
        # The value under a handle is that handle's validated model by
        # construction; a heterogeneous mapping keyed by GQLFragment[T] with
        # T-valued entries is not expressible, hence the cast at this single
        # boundary.
        _, data = entry
        return cast("TData | None", data)

    def add_slot_data__(
        self, handle: GQLFragment[pydantic.BaseModel], data: object
    ) -> None:
        # Written only by `validate_slot__`; the `__` suffix marks it as the
        # slot runtime's own contract, like `slot_name__`.
        self._slot_data[id(handle)] = (handle, data)

    @pydantic.model_validator(mode="wrap")
    @classmethod
    def validate_slot__(
        cls,
        data: object,
        handler: pydantic.ValidatorFunctionWrapHandler,
        info: pydantic.ValidationInfo,
    ) -> Self:
        if not _is_payload(data):
            # Not an object payload: let regular model validation report it.
            # pydantic types the wrap handler's return as Any; the annotations
            # pin it back to the model being validated.
            unchanged: Self = handler(data)  # pyright: ignore[reportAny]
            return unchanged
        # The payload carries every passed fragment's fields next to the
        # static selection; the node's own model ignores the extras (the
        # generated open config), and each fragment's model picks out its own.
        node: Self = handler(data)  # pyright: ignore[reportAny]
        typename = data.get("__typename")
        # ValueError, not TypeError: pydantic-core only wraps
        # ValueError/AssertionError raised from a wrap validator into a
        # ValidationError placed at the node's response path, and a raw
        # TypeError would escape with no path at all. `_is_typename` keeps the
        # guard clause off ruff's TRY004, which reads a bare `isinstance` test
        # around a raise as a type check that wants TypeError.
        if not _is_typename(typename):
            msg = f"slot {cls.slot_name__!r} payload is missing __typename"
            raise ValueError(msg)
        for handle in _slot_handles(info, cls.slot_name__):
            # Every offered handle gets an entry — None on a typename
            # mismatch — so `slot_data__` can tell "never offered" apart. A
            # covered_typenames__ miss can only be a mismatch, never schema
            # drift: the slot node's Literal typename has already rejected
            # drift by the time this check runs (see the uniform-branch
            # comment in codegen/collect._collect_polymorphic_models and its
            # test).
            node.add_slot_data__(
                handle,
                handle.validate__(data)
                if typename in handle.covered_typenames__
                else None,
            )
        return node


def _slot_handles(
    info: pydantic.ValidationInfo, slot_name: str
) -> tuple[GQLFragment[pydantic.BaseModel], ...]:
    context = info.context
    if not isinstance(context, dict) or slot_name not in context:
        msg = f"slot {slot_name!r} validated without a fragments context"
        raise ValueError(msg)
    # Only this slot's own entry is checked: the context is pydantic's
    # general-purpose channel, and entries the caller's own validators put
    # there are none of this slot's business. A malformed value under the
    # slot's key is still diagnosed precisely instead of surfacing as an
    # AttributeError from inside the handle loop.
    entry = cast("dict[object, object]", context)[slot_name]
    if not _is_handles(entry):
        msg = f"slot {slot_name!r} context entry is not a tuple of fragment handles"
        raise ValueError(msg)
    return entry


def as_handles(
    value: GQLFragment[pydantic.BaseModel] | Sequence[GQLFragment[pydantic.BaseModel]],
) -> tuple[GQLFragment[pydantic.BaseModel], ...]:
    if isinstance(value, GQLFragment):
        return (value,)
    return tuple(value)


def build_slot_source(
    head: str,
    splices: Sequence[tuple[str, str]],
    slot_fragments: SlotFragments,
) -> str:
    # One (slot name, following text) entry per @slot occurrence; splicing is
    # purely positional — see `codegen/slots.build_exec_parts` for why.
    # Definitions are keyed by fragment name: one handle passed to several
    # slots still contributes its definition once.
    definitions: dict[str, str] = {}
    chunks: list[str] = [head]
    for slot_name, part in splices:
        handles = sorted(
            slot_fragments[slot_name], key=lambda handle: handle.fragment_name__
        )
        spreads = " ".join(f"...{handle.fragment_name__}" for handle in handles)
        chunks.extend((spreads, part))
        for handle in handles:
            definitions[handle.fragment_name__] = handle.fragment_def__
    body = "".join(chunks)
    if not definitions:
        return body
    ordered = "\n\n".join(definitions[name] for name in sorted(definitions))
    return f"{body}\n\n{ordered}"
