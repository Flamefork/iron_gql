import copy
import pickle
from typing import Annotated
from typing import ClassVar
from typing import Literal
from typing import cast
from typing import override

import pydantic
import pytest

from iron_gql import slots
from iron_gql.codegen.names import SLOT_RUNTIME_FIELD_NAMES

# The only object type this scaffold's slots ever resolve to.
IMAGE_TYPENAMES = frozenset({"Image"})


def offered(
    *handles: slots.GQLFragment[pydantic.BaseModel],
    typenames: frozenset[str] = IMAGE_TYPENAMES,
) -> tuple[slots.SlotHandle, ...]:
    # What the generator renders into `slot_handles__`. The default is what a
    # fragment bound straight into one of this scaffold's slots gets; pass
    # `typenames` to model a fragment the slot only reaches through a narrowing
    # spread, or one whose type condition has drifted from the schema.
    return tuple(slots.SlotHandle(handle, typenames) for handle in handles)


class Model(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", populate_by_name=True)


# Mirrors the generated scaffold: models validating inside a slot or fragment
# subtree ignore the keys other readers asked for; the rest stay strict.
class OpenModel(Model):
    model_config = pydantic.ConfigDict(extra="ignore")


class SlotModel[TOffered](OpenModel, slots.GQLSlotNode[TOffered]):
    pass


# The phantom this scaffold's nodes carry: the erased handle type, so a node
# stands for a binding that offered whatever handle a test hands it. A
# generated package pins the concrete fragment classes instead -- that is the
# static half, covered by tests/test_slots_typing.py -- while the wiring
# guards exercised here are what a type-erased path still runs into.
type AnyFragment = slots.GQLFragment[pydantic.BaseModel]


class DetailsSlot(SlotModel[AnyFragment]):
    slot_name__: ClassVar[str] = "details"
    typename__: Annotated[str, pydantic.Field(validation_alias="__typename")]


class UrlData(OpenModel):
    url: Annotated[str, pydantic.Field(validation_alias="url")]


class Result(Model):
    details: list[DetailsSlot | None]


URL_HANDLE = slots.GQLFragment(
    fragment_name="UrlFragment",
    adapter=pydantic.TypeAdapter(UrlData),
)


class NoTypenameFieldSlot(SlotModel[AnyFragment]):
    slot_name__: ClassVar[str] = "empty"


class NoTypenameResult(Model):
    empty: NoTypenameFieldSlot


class OwnerData(OpenModel):
    id: str


class NestedSlot(SlotModel[AnyFragment]):
    slot_name__: ClassVar[str] = "nested"
    typename__: Annotated[str, pydantic.Field(validation_alias="__typename")]
    owner: OwnerData


class NestedResult(Model):
    nested: NestedSlot


def test_eager_validation_fills_every_slot_node():
    result = Result.model_validate(
        {
            "details": [
                {"__typename": "Image", "url": "a"},
                {"__typename": "Image", "url": "b"},
            ]
        },
        context={"details": offered(URL_HANDLE)},
    )
    assert [URL_HANDLE.read(node) for node in result.details] == [
        UrlData(url="a"),
        UrlData(url="b"),
    ]


def test_read_answers_none_for_foreign_typename_and_null_nodes():
    result = Result.model_validate(
        {"details": [{"__typename": "Link", "token": "t"}, None]},
        context={"details": offered(URL_HANDLE)},
    )
    assert URL_HANDLE.read(result.details[0]) is None
    assert URL_HANDLE.read(result.details[1]) is None


def test_slot_node_requires_its_own_selection():
    # Extra keys are ignored (they belong to the passed fragments), but the
    # static selection's own fields stay required.
    with pytest.raises(pydantic.ValidationError, match="__typename"):
        Result.model_validate(
            {"details": [{"url": "a"}]},
            context={"details": offered(URL_HANDLE)},
        )


def test_broken_fragment_data_raises_with_response_path():
    with pytest.raises(pydantic.ValidationError) as exc_info:
        Result.model_validate(
            {"details": [{"__typename": "Image"}]},
            context={"details": offered(URL_HANDLE)},
        )
    assert exc_info.value.errors()[0]["loc"] == ("details", 0, "url")


def test_missing_context_is_loud():
    with pytest.raises(pydantic.ValidationError, match="without a fragments context"):
        Result.model_validate({"details": [{"__typename": "Image", "url": "a"}]})


def test_missing_typename_in_payload_surfaces_as_validation_error():
    # NoTypenameFieldSlot declares no __typename model field (unlike
    # DetailsSlot), so `handler(...)` can't catch a missing __typename on its
    # own — the wrap validator's own check is the only thing that can, and
    # this is what actually reaches it.
    with pytest.raises(pydantic.ValidationError) as exc_info:
        NoTypenameResult.model_validate(
            {"empty": {}},
            context={"empty": ()},
        )
    assert exc_info.value.errors()[0]["loc"] == ("empty",)


class ClosedTypenameSlot(SlotModel[AnyFragment]):
    slot_name__: ClassVar[str] = "closed"
    typename__: Annotated[
        Literal["Image"], pydantic.Field(validation_alias="__typename")
    ]


class ClosedTypenameResult(Model):
    closed: ClosedTypenameSlot


def test_drifted_typename_is_rejected_before_any_handle_validates():
    # The line order inside `validate_slot__` is a behavioral guarantee: the
    # node's own closed typename rejects schema drift before the handle loop
    # runs, so a handle whose typenames admit a drifted typename can never
    # surface its own validation error instead of the drift. If the loop ran
    # first, the drifted handle below would fail on its missing `url` and that
    # error — not the literal mismatch — would reach the caller.
    drifted = slots.GQLFragment(
        fragment_name="Drifted",
        adapter=pydantic.TypeAdapter(UrlData),
    )
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ClosedTypenameResult.model_validate(
            {"closed": {"__typename": "Bot"}},
            context={"closed": offered(drifted, typenames=frozenset({"Bot"}))},
        )
    error = exc_info.value.errors()[0]
    assert error["loc"] == ("closed", "__typename")
    assert error["type"] == "literal_error"


def test_scalar_where_a_selection_is_expected_arrives_with_a_response_path():
    # A malformed nested payload is ordinary model validation: the error
    # carries the full path into the response instead of a bare exception.
    with pytest.raises(pydantic.ValidationError) as exc_info:
        NestedResult.model_validate(
            {"nested": {"__typename": "Image", "owner": "oops"}},
            context={"nested": ()},
        )
    error = exc_info.value.errors()[0]
    assert error["loc"] == ("nested", "owner")
    assert error["type"] == "model_type"


def test_non_object_slot_payload_defers_to_model_validation():
    # No __typename to dispatch handles on: the wrap validator hands the
    # payload to pydantic untouched and lets the ordinary "not a model" error
    # stand.
    with pytest.raises(pydantic.ValidationError) as exc_info:
        Result.model_validate(
            {"details": ["oops"]}, context={"details": offered(URL_HANDLE)}
        )
    error = exc_info.value.errors()[0]
    assert error["loc"][:2] == ("details", 0)
    assert error["type"] == "model_type"


def test_slot_node_instance_is_passed_through_with_its_slot_data():
    # The other half of the same branch: an already-validated node is not a
    # payload either, and pydantic accepts it as-is, together with the slot
    # data it already carries.
    handles = offered(URL_HANDLE)
    node = DetailsSlot.model_validate(
        {"__typename": "Image", "url": "a"}, context={"details": handles}
    )
    result = Result.model_validate({"details": [node]}, context={"details": handles})
    assert result.details[0] is node
    assert URL_HANDLE.read(result.details[0]) == UrlData(url="a")


def test_slot_data_survives_deep_copies_of_a_validated_node():
    # deepcopy and model_copy(deep=True) walk the private attributes; a
    # handle is an identity token (`__deepcopy__` returns self) and entries
    # are keyed by id(), so the reader's own reference still finds its data
    # in the clone.
    result = Result.model_validate(
        {"details": [{"__typename": "Image", "url": "a"}]},
        context={"details": offered(URL_HANDLE)},
    )
    for clone in (copy.deepcopy(result), result.model_copy(deep=True)):
        assert URL_HANDLE.read(clone.details[0]) == UrlData(url="a")


def test_copying_a_fragment_handle_preserves_its_identity():
    # `__copy__` guards the same invariant as `__deepcopy__`, for the shallow
    # protocol: a handle copied out from under the module-level singleton
    # would no longer share its id() with the entries `slot_data__` looks up
    # by, turning a legitimate read into a false "not part of the binding".
    assert copy.copy(URL_HANDLE) is URL_HANDLE


def test_slot_data_survives_in_process_pickling():
    # Pickling recreates the stored handle objects, but entries stay keyed by
    # the reading handle's id(), which is alive in this process — the
    # round-trip must not turn into a false "is not part of the binding".
    # The pickle payload is produced and consumed inside this test, so
    # unpickling is safe here.
    result = Result.model_validate(
        {"details": [{"__typename": "Image", "url": "a"}]},
        context={"details": offered(URL_HANDLE)},
    )
    # `pickle.loads` answers `Any` -- a payload holds nothing to type it by --
    # and the isinstance below is what turns it back into a type: the check
    # this test wants anyway, made where the `Any` would otherwise spread.
    restored = cast("object", pickle.loads(pickle.dumps(result)))
    assert isinstance(restored, Result)
    assert URL_HANDLE.read(restored.details[0]) == UrlData(url="a")


def test_foreign_context_entries_coexist_with_slot_handles():
    # The validation context is pydantic's general-purpose channel: entries
    # the caller's own validators need must not disable slot validation.
    result = Result.model_validate(
        {"details": [{"__typename": "Image", "url": "a"}]},
        context={"details": offered(URL_HANDLE), "locale": "en"},
    )
    assert URL_HANDLE.read(result.details[0]) == UrlData(url="a")


def test_malformed_entry_under_the_slot_key_is_diagnosed():
    # A wrong-shaped value under the slot's own key is named precisely — not
    # "without a fragments context" (it was passed), and not an
    # AttributeError from inside the handle loop.
    with pytest.raises(pydantic.ValidationError, match="context entry is not a tuple"):
        Result.model_validate(
            {"details": [{"__typename": "Image", "url": "a"}]},
            context={"details": [URL_HANDLE]},
        )


def test_handle_identity_ignores_subclass_equality():
    # A subclass may define __eq__/__hash__ for its own purposes; slot data
    # stays keyed by object identity, so an equal-but-different handle must
    # not slip past "is not part of the binding" into another handle's data.
    class NamedFragment(slots.GQLFragment[UrlData]):
        def __init__(self) -> None:
            super().__init__(
                fragment_name="Named",
                adapter=pydantic.TypeAdapter(UrlData),
            )

        @override
        def __eq__(self, other: object) -> bool:
            return isinstance(other, NamedFragment)

        @override
        def __hash__(self) -> int:
            return hash("Named")

    passed = NamedFragment()
    twin = NamedFragment()
    result = Result.model_validate(
        {"details": [{"__typename": "Image", "url": "a"}]},
        context={"details": offered(passed)},
    )
    assert passed.read(result.details[0]) == UrlData(url="a")
    with pytest.raises(ValueError, match="is not part of the binding"):
        twin.read(result.details[0])


class OtherData(OpenModel):
    href: Annotated[str, pydantic.Field(validation_alias="href")]


OTHER = slots.GQLFragment(
    fragment_name="OtherFragment",
    adapter=pydantic.TypeAdapter(OtherData),
)


def test_models_expose_only_their_own_selection():
    # The payload under a slot carries the static selection plus every passed
    # fragment's fields. The node's model and each fragment's model pick out
    # exactly their own fields — nothing of another reader's selection is
    # reachable, dumped or stored.
    result = Result.model_validate(
        {"details": [{"__typename": "Image", "url": "a", "href": "x"}]},
        context={"details": offered(URL_HANDLE, OTHER)},
    )
    node = result.details[0]
    assert node is not None
    assert node.model_dump() == {"typename__": "Image"}
    assert URL_HANDLE.read(node) == UrlData(url="a")
    assert OTHER.read(node) == OtherData(href="x")


def test_slot_runtime_field_names_match_the_runtime_class():
    # The codegen-side reserved tuple is the canonical list of names the slot
    # runtime claims on every GQLSlotNode subclass; set equality pins both
    # directions, so neither a stale entry here nor a new contract name on
    # the class can drift silently. The class's own contract names are its
    # annotations plus every `name__`-suffixed member — the suffix is the
    # documented marker of the slot runtime's contract.
    contract_names = set(slots.GQLSlotNode.__annotations__) | {
        name
        for name in vars(slots.GQLSlotNode)
        if name.endswith("__") and not name.startswith("__")
    }
    assert contract_names == set(SLOT_RUNTIME_FIELD_NAMES)
