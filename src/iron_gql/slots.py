from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from enum import auto
from types import MappingProxyType
from typing import Any
from typing import ClassVar
from typing import Generic
from typing import Never
from typing import Self
from typing import TypeIs
from typing import TypeVar
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


type FragmentDefinitionType = type[GQLFragment[pydantic.BaseModel, Any]]
type BindableFragmentType = type[GQLBindableFragment[pydantic.BaseModel, Any]]
type SlotReaders = Mapping[str, tuple[SlotReader, ...]]

# A module-level singleton rather than `MappingProxyType({})` written inline
# as the default: basedpyright's `all` mode rejects a call in a parameter
# default outright (`reportCallInDefaultInitializer`), whatever the callee
# returns -- naming it here is what a plain fragment's constructor call
# resolves to instead, with the same one-empty-mapping-for-every-plain-
# fragment behavior.
_NO_ARGS: Mapping[str, object] = MappingProxyType({})


class Omitted(Enum):
    OMITTED = auto()


OMITTED = Omitted.OMITTED


TModel_co = TypeVar("TModel_co", bound=pydantic.BaseModel, covariant=True)
# A fragment's readable closure: the fragment itself plus every fragment from
# its root-level spreads, intersected over all compatible slots. The parameter
# is contravariant because `read` accepts a node that offers this closure. PEP
# 695 does not allow declaring variance, hence UP046.
TReads_contra = TypeVar("TReads_contra", contravariant=True)


class GQLFragment(Generic[TModel_co, TReads_contra]):  # noqa: UP046
    # Metadata lives on the instance, taken as required constructor
    # arguments: `TypeAdapter` is invariant in its parameter, so an adapter
    # that does not match `TModel_co` is a static error at the generated
    # `super().__init__` call, and a subclass that forgets its metadata
    # cannot even be instantiated — neither invariant needs a runtime check.
    def __init__(
        self,
        *,
        fragment_name: str,
        definition_type: "type[GQLFragment[TModel_co, TReads_contra]]",
        adapter: pydantic.TypeAdapter[TModel_co],
    ) -> None:
        self.fragment_name__ = fragment_name
        self._definition_type = definition_type
        # Only the bound validate function is stored: a `TypeAdapter[TModel_co]`
        # attribute would put TModel_co in an invariant position and destroy
        # the inferred covariance that lets shared code accept
        # `SomeFragment[pydantic.BaseModel]`; in return position TModel_co
        # keeps the class covariant and `validate__` fully typed.
        self._validate: Callable[[object], TModel_co] = adapter.validate_python

    def validate__(self, payload: object) -> TModel_co:
        return self._validate(payload)

    @property
    def definition_type(
        self,
    ) -> "type[GQLFragment[TModel_co, TReads_contra]]":
        return self._definition_type

    # Codegen states on each node the types of the fragment readers available
    # there, so the contravariant contract rejects a reader for a node it was
    # not offered. On the type-erased path the same wiring bug raises
    # `ValueError` in `slot_data__`.
    #
    # Code generic over which binding it was handed spells the phantom `Any`
    # (`{Op}Result[Any]`), which this signature accepts like any other: giving
    # up the check is a decision written at the annotation, not a second read
    # method to choose between.
    def read(self, node: "GQLSlotNode[TReads_contra] | None") -> TModel_co | None:
        if node is None:
            return None
        return node.slot_data__(self)


class GQLBindableFragment(GQLFragment[TModel_co, TReads_contra]):
    def __init__(
        self,
        *,
        fragment_name: str,
        definition_type: type[GQLFragment[TModel_co, TReads_contra]],
        adapter: pydantic.TypeAdapter[TModel_co],
    ) -> None:
        super().__init__(
            fragment_name=fragment_name,
            definition_type=definition_type,
            adapter=adapter,
        )
        self._fragment_args = _NO_ARGS

    def _set_fragment_args(self, fragment_args: Mapping[str, object]) -> None:
        self._fragment_args = MappingProxyType(dict(fragment_args))

    @property
    def fragment_args__(self) -> Mapping[str, object]:
        return self._fragment_args


@dataclass(frozen=True, slots=True)
class SlotReader:
    # One fragment available in one slot of one binding, and the runtime
    # typenames for which its fields are present in that slot's root payload.
    #
    # Coverage belongs to a specific binding and slot, not just to the fragment:
    # one fragment can reach the root of the first slot directly and go through
    # a narrower condition in the second. Validation outside the narrow set
    # would then reject a correct response where the server did not send these
    # fields.
    definition: GQLFragment[pydantic.BaseModel, Any]
    typenames: frozenset[str]


def _is_readers(value: object) -> TypeIs[tuple[SlotReader, ...]]:
    if not isinstance(value, tuple):
        return False
    readers = cast("tuple[object, ...]", value)
    return all(isinstance(reader, SlotReader) for reader in readers)


# The "offered fragments" phantom: a union of the fragment reader types
# available on this node. Codegen passes it through every model on the path to
# the node, and a binding substitutes the value into its own result type.
# Contravariance lets a node with a larger set of fragments be used where a
# smaller one is expected: `Slot[A | B]` is compatible with `GQLSlotNode[A]`.
# PEP 695 does not allow setting variance explicitly and infers an unused
# phantom as covariant, so a plain TypeVar and the UP046 suppression are needed
# here. The `Never` default means a bare `GQLSlotNode` is readable by no
# fragment.
TOffered_contra = TypeVar("TOffered_contra", contravariant=True, default=Never)


class GQLSlotNode(pydantic.BaseModel, Generic[TOffered_contra]):  # noqa: UP046
    slot_name__: ClassVar[str]

    # The projection belongs to the generated fragment definition, not to a
    # specific definition value or a factory application from `bind()`. So
    # runtime identity is set by the exact generated class.
    _slot_data: dict[FragmentDefinitionType, object] = pydantic.PrivateAttr(
        default_factory=dict
    )

    def slot_data__[TData: pydantic.BaseModel, TReads](
        self, definition: GQLFragment[TData, TReads]
    ) -> TData | None:
        # Membership is checked separately from the value: `None` is allowed for
        # an offered definition whose typename does not cover this node.
        definition_type = definition.definition_type
        if definition_type not in self._slot_data:
            msg = (
                f"fragment '{definition.fragment_name__}' is not part of the "
                f"binding that produced slot '{self.slot_name__}'"
            )
            raise ValueError(msg)
        # By construction the value under a definition class is that
        # definition's validated model. The heterogeneous link cannot be
        # expressed by the mapping's type, so the cast stays at this boundary.
        return cast("TData | None", self._slot_data[definition_type])

    def add_slot_data__(
        self, definition: GQLFragment[pydantic.BaseModel, Any], data: object
    ) -> None:
        # Written only by `validate_slot__`; the `__` suffix marks it as the
        # slot runtime's own contract, like `slot_name__`.
        self._slot_data[definition.definition_type] = data

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
        for reader in _slot_readers(info, cls.slot_name__):
            # Every offered definition gets an entry -- `None` on a typename
            # mismatch. A missing entry unambiguously means "was not offered".
            # Schema drift is already rejected by the slot node's Literal
            # typename.
            node.add_slot_data__(
                reader.definition,
                reader.definition.validate__(data)
                if typename in reader.typenames
                else None,
            )
        return node


def _slot_readers(
    info: pydantic.ValidationInfo, slot_name: str
) -> tuple[SlotReader, ...]:
    context = info.context
    if not isinstance(context, Mapping) or slot_name not in context:
        msg = f"slot {slot_name!r} validated without a fragments context"
        raise ValueError(msg)
    # Only this slot's entry is checked: the validation context is a shared
    # pydantic channel and may hold data of the caller's validators. An invalid
    # value under the slot key is diagnosed here, not as an `AttributeError`
    # inside the reader loop.
    entry = cast("Mapping[object, object]", context)[slot_name]
    if not _is_readers(entry):
        msg = f"slot {slot_name!r} context entry is not a tuple of slot readers"
        raise ValueError(msg)
    return entry


def as_bindable_fragments(
    value: GQLBindableFragment[pydantic.BaseModel, Any]
    | Sequence[GQLBindableFragment[pydantic.BaseModel, Any]],
) -> tuple[GQLBindableFragment[pydantic.BaseModel, Any], ...]:
    # Normalizes the two allowed forms of a filled slot -- one bindable fragment
    # or a sequence of them -- into a single tuple. The function is public
    # because the generated `bind()` passes this form to the `passed` argument
    # of `bound__`; the same normalization is needed below for `binding_key`.
    if isinstance(value, GQLBindableFragment):
        return (value,)
    return tuple(value)


type CombinationKey = tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]
type BindingKey = tuple[
    tuple[
        str,
        tuple[BindableFragmentType, ...],
    ],
    ...,
]


def combination_key(
    template: str, slots: Iterable[tuple[str, Iterable[str]]]
) -> CombinationKey:
    # The canonical form of a logical combination, which codegen builds from the
    # IR. An empty slot disappears, and slots and fragment names are sorted, so
    # the order in the source bind call does not affect the combination's
    # identity.
    #
    # What this normalisation must never be asked to do is tell a *misspelled*
    # slot from a real one, since it erases exactly the evidence: names are
    # checked before a key is taken, by the generated signature at runtime and
    # by `expand_binding` at generation. `tests/test_bind_contract.py` pins
    # both halves.
    entries = [(slot, tuple(sorted(names))) for slot, names in slots]
    return (template, tuple((slot, names) for slot, names in sorted(entries) if names))


def binding_key(
    fragments: Mapping[
        str,
        GQLBindableFragment[pydantic.BaseModel, Any]
        | Sequence[GQLBindableFragment[pydantic.BaseModel, Any]],
    ],
) -> BindingKey:
    # A runtime binding identifies a bindable fragment by its exact generated
    # class. For a plain fragment that is the public definition class, for a
    # factory the private applied class. Reader identity through
    # `definition_type` remains a separate contract.
    entries = [
        (
            slot,
            tuple(
                sorted(
                    (type(fragment) for fragment in as_bindable_fragments(raw)),
                    key=lambda fragment_class: (
                        fragment_class.__module__,
                        fragment_class.__qualname__,
                    ),
                )
            ),
        )
        for slot, raw in fragments.items()
    ]
    return tuple(
        (slot, fragment_classes)
        for slot, fragment_classes in sorted(entries)
        if fragment_classes
    )
