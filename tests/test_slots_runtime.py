import copy
import pickle
from typing import Annotated
from typing import ClassVar
from typing import Literal
from typing import override

import pydantic
import pytest

from iron_gql import slots
from iron_gql.codegen.names import SLOT_RUNTIME_FIELD_NAMES


class Model(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", populate_by_name=True)


# Mirrors the generated scaffold: models validating inside a slot or fragment
# subtree ignore the keys other readers asked for; the rest stay strict.
class OpenModel(Model):
    model_config = pydantic.ConfigDict(extra="ignore")


class SlotModel(OpenModel, slots.GQLSlotNode):
    pass


class DetailsSlot(SlotModel):
    slot_name__: ClassVar[str] = "details"
    typename__: Annotated[str, pydantic.Field(validation_alias="__typename")]


class UrlData(OpenModel):
    url: Annotated[str, pydantic.Field(validation_alias="url")]


class Result(Model):
    details: list[DetailsSlot | None]


URL_HANDLE = slots.GQLFragment(
    fragment_name="UrlFragment",
    fragment_def="fragment UrlFragment on Image {\n  url\n}",
    covered_typenames=frozenset({"Image"}),
    adapter=pydantic.TypeAdapter(UrlData),
)


class NoTypenameFieldSlot(SlotModel):
    slot_name__: ClassVar[str] = "empty"


class NoTypenameResult(Model):
    empty: NoTypenameFieldSlot


class OwnerData(OpenModel):
    id: str


class NestedSlot(SlotModel):
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
        context={"details": slots.as_handles(URL_HANDLE)},
    )
    assert [URL_HANDLE.read(node) for node in result.details] == [
        UrlData(url="a"),
        UrlData(url="b"),
    ]


def test_read_answers_none_for_foreign_typename_and_null_nodes():
    result = Result.model_validate(
        {"details": [{"__typename": "Link", "token": "t"}, None]},
        context={"details": slots.as_handles([URL_HANDLE])},
    )
    assert URL_HANDLE.read(result.details[0]) is None
    assert URL_HANDLE.read(result.details[1]) is None


def test_slot_node_requires_its_own_selection():
    # Extra keys are ignored (they belong to the passed fragments), but the
    # static selection's own fields stay required.
    with pytest.raises(pydantic.ValidationError, match="__typename"):
        Result.model_validate(
            {"details": [{"url": "a"}]},
            context={"details": slots.as_handles(URL_HANDLE)},
        )


def test_broken_fragment_data_raises_with_response_path():
    with pytest.raises(pydantic.ValidationError) as exc_info:
        Result.model_validate(
            {"details": [{"__typename": "Image"}]},
            context={"details": slots.as_handles(URL_HANDLE)},
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


class ClosedTypenameSlot(SlotModel):
    slot_name__: ClassVar[str] = "closed"
    typename__: Annotated[
        Literal["Image"], pydantic.Field(validation_alias="__typename")
    ]


class ClosedTypenameResult(Model):
    closed: ClosedTypenameSlot


def test_drifted_typename_is_rejected_before_any_handle_validates():
    # The line order inside `validate_slot__` is a behavioral guarantee: the
    # node's own closed typename rejects schema drift before the handle loop
    # runs, so a covered_typenames__ hit on a drifted typename can never
    # surface the handle's own validation error instead of the drift. If the
    # loop ran first, the drifted handle below would fail on its missing
    # `url` and that error — not the literal mismatch — would reach the
    # caller.
    drifted = slots.GQLFragment(
        fragment_name="Drifted",
        fragment_def="fragment Drifted on Bot {\n  url\n}",
        covered_typenames=frozenset({"Bot"}),
        adapter=pydantic.TypeAdapter(UrlData),
    )
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ClosedTypenameResult.model_validate(
            {"closed": {"__typename": "Bot"}},
            context={"closed": slots.as_handles(drifted)},
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
            {"details": ["oops"]}, context={"details": slots.as_handles(URL_HANDLE)}
        )
    error = exc_info.value.errors()[0]
    assert error["loc"][:2] == ("details", 0)
    assert error["type"] == "model_type"


def test_slot_node_instance_is_passed_through_with_its_slot_data():
    # The other half of the same branch: an already-validated node is not a
    # payload either, and pydantic accepts it as-is, together with the slot
    # data it already carries.
    handles = slots.as_handles(URL_HANDLE)
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
        context={"details": slots.as_handles(URL_HANDLE)},
    )
    for clone in (copy.deepcopy(result), result.model_copy(deep=True)):
        assert URL_HANDLE.read(clone.details[0]) == UrlData(url="a")


def test_slot_data_survives_in_process_pickling():
    # Pickling recreates the stored handle objects, but entries stay keyed by
    # the reading handle's id(), which is alive in this process — the
    # round-trip must not turn into a false "was not passed". The pickle
    # payload is produced and consumed inside this test, so unpickling is
    # safe here.
    result = Result.model_validate(
        {"details": [{"__typename": "Image", "url": "a"}]},
        context={"details": slots.as_handles(URL_HANDLE)},
    )
    restored: object = pickle.loads(pickle.dumps(result))  # pyright: ignore[reportAny]
    assert isinstance(restored, Result)
    assert URL_HANDLE.read(restored.details[0]) == UrlData(url="a")


def test_foreign_context_entries_coexist_with_slot_handles():
    # The validation context is pydantic's general-purpose channel: entries
    # the caller's own validators need must not disable slot validation.
    result = Result.model_validate(
        {"details": [{"__typename": "Image", "url": "a"}]},
        context={"details": slots.as_handles(URL_HANDLE), "locale": "en"},
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
    # not slip past "was not passed" into another handle's data.
    class NamedFragment(slots.GQLFragment[UrlData]):
        def __init__(self) -> None:
            super().__init__(
                fragment_name="Named",
                fragment_def="fragment Named on Image {\n  url\n}",
                covered_typenames=frozenset({"Image"}),
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
        context={"details": slots.as_handles(passed)},
    )
    assert passed.read(result.details[0]) == UrlData(url="a")
    with pytest.raises(ValueError, match="was not passed"):
        twin.read(result.details[0])


class OtherData(OpenModel):
    href: Annotated[str, pydantic.Field(validation_alias="href")]


OTHER = slots.GQLFragment(
    fragment_name="OtherFragment",
    fragment_def="fragment OtherFragment on Image {\n  href\n  id\n}",
    covered_typenames=frozenset({"Image"}),
    adapter=pydantic.TypeAdapter(OtherData),
)


EXTRA = slots.GQLFragment(
    fragment_name="ExtraFragment",
    fragment_def="fragment ExtraFragment on Image {\n  id\n}",
    covered_typenames=frozenset({"Image"}),
    adapter=pydantic.TypeAdapter(OtherData),
)


def test_models_expose_only_their_own_selection():
    # The payload under a slot carries the static selection plus every passed
    # fragment's fields. The node's model and each fragment's model pick out
    # exactly their own fields — nothing of another reader's selection is
    # reachable, dumped or stored.
    result = Result.model_validate(
        {"details": [{"__typename": "Image", "url": "a", "href": "x"}]},
        context={"details": slots.as_handles([URL_HANDLE, OTHER])},
    )
    node = result.details[0]
    assert node is not None
    assert node.model_dump() == {"typename__": "Image"}
    assert URL_HANDLE.read(node) == UrlData(url="a")
    assert OTHER.read(node) == OtherData(href="x")


# The printed operation pre-split at the slot's marker field — the head plus
# one (slot name, following text) pair per gap — the way codegen hands it to
# the runtime.
HEAD = "query Q {\n  ds {\n    details {\n      __typename\n      "
SPLICES = (("details", "\n    }\n  }\n}"),)


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


def test_build_slot_source_inserts_spreads_and_definitions():
    built = slots.build_slot_source(
        HEAD, SPLICES, {"details": slots.as_handles(URL_HANDLE)}
    )
    assert "...UrlFragment" in built
    assert built.endswith("fragment UrlFragment on Image {\n  url\n}")


def test_build_slot_source_is_order_independent():
    forward = slots.build_slot_source(
        HEAD, SPLICES, {"details": slots.as_handles([OTHER, EXTRA])}
    )
    backward = slots.build_slot_source(
        HEAD, SPLICES, {"details": slots.as_handles([EXTRA, OTHER])}
    )
    assert forward == backward


def test_build_slot_source_without_fragments_joins_the_parts_bare():
    built = slots.build_slot_source(HEAD, SPLICES, {"details": ()})
    assert built == HEAD + SPLICES[0][1]
    assert "fragment" not in built


def test_build_slot_source_splices_each_slot_at_its_own_gap():
    built = slots.build_slot_source(
        "query Q {\n  details {\n    ",
        (
            ("details", "\n  }\n  detailsExtra {\n    "),
            ("detailsExtra", "\n  }\n}"),
        ),
        {
            "details": slots.as_handles(URL_HANDLE),
            "detailsExtra": slots.as_handles(OTHER),
        },
    )
    assert "details {\n    ...UrlFragment\n  }" in built
    assert "detailsExtra {\n    ...OtherFragment\n  }" in built


def test_build_slot_source_ships_a_handle_on_two_slots_once():
    # Definitions are keyed by fragment name: one handle passed to several
    # slots is spread at each gap but contributes its definition text once.
    built = slots.build_slot_source(
        "query Q {\n  details {\n    ",
        (
            ("details", "\n  }\n  detailsExtra {\n    "),
            ("detailsExtra", "\n  }\n}"),
        ),
        {
            "details": slots.as_handles(URL_HANDLE),
            "detailsExtra": slots.as_handles(URL_HANDLE),
        },
    )
    assert built.count("...UrlFragment") == 2
    assert built.count("fragment UrlFragment on Image") == 1
