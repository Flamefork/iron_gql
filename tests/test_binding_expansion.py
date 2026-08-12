import graphql
import pytest

from iron_gql.codegen import GraphQLGenerationError
from iron_gql.codegen.bindings import ExpandedBinding
from iron_gql.codegen.bindings import OmittableSynthesizedVar
from iron_gql.codegen.bindings import RequiredSynthesizedVar
from iron_gql.codegen.bindings import SlotTarget
from iron_gql.codegen.bindings import expand_binding


def _schema(*image_fields: str) -> str:
    # Every schema in this file is the same shape -- a post with a union
    # attachment -- and differs only in the argument-carrying fields
    # `ImageAttachment` offers the fragments under test. Written as one
    # template so each constant below shows exactly what its own case needs.
    fields = "\n".join(f"    {field}" for field in image_fields)
    return f"""
type Query {{
    post(id: ID!): Post
}}

type Post {{
    id: ID!
    attachment: Attachment
}}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {{
    url: String!
{fields}
}}

type LinkAttachment {{
    href: String!
}}
"""


SCHEMA = _schema("caption: String!")

TEMPLATE = """
query GetAttachment($id: ID!) {
    post(id: $id) {
        id
        attachment @slot { __typename }
    }
}
"""

# Keyed by the `bind()` keyword, which is also the response key here.
SLOTS = {"attachment": SlotTarget(type_name="Attachment", response_key="attachment")}


def _only_fragment(doc: graphql.DocumentNode) -> graphql.FragmentDefinitionNode:
    [fragment] = doc.definitions
    assert isinstance(fragment, graphql.FragmentDefinitionNode)
    return fragment


def _fragment(text: str) -> graphql.FragmentDefinitionNode:
    return _only_fragment(graphql.parse(text))


def _direct(expanded: ExpandedBinding) -> dict[str, tuple[str, ...]]:
    # What the bind named per slot, as `ReadableFragment.direct` records it.
    return {
        key: tuple(entry.name for entry in entries if entry.direct)
        for key, entries in expanded.readable_fragments.items()
    }


def _operation(doc: graphql.DocumentNode) -> graphql.OperationDefinitionNode:
    [operation] = [
        definition
        for definition in doc.definitions
        if isinstance(definition, graphql.OperationDefinitionNode)
    ]
    return operation


def test_expansion_inserts_spreads_strips_slot_appends_defs():
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { url }")

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts},
        location="test:1",
    )

    assert "@slot" not in expanded.exec_source
    assert "...ImageParts" in expanded.exec_source
    assert "fragment ImageParts on ImageAttachment" in expanded.exec_source
    assert _direct(expanded) == {"attachment": ("ImageParts",)}
    assert expanded.fragment_vars == ()


SCHEMA_WITH_LIMIT_ARG = _schema(
    "photos(limit: Int!): [String!]!",
    "morePhotos(limit: Int!): [String!]!",
)


def test_fragment_variable_declaration_synthesized_and_required():
    # `$limit` is used twice, at two agreeing `Int!` positions, so this also
    # pins that a second matching usage does not spuriously raise a conflict.
    schema = graphql.build_schema(SCHEMA_WITH_LIMIT_ARG)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment("""
        fragment ImageParts on ImageAttachment {
            photos(limit: $limit)
            morePhotos(limit: $limit)
        }
        """)

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts},
        location="test:1",
    )

    assert "($id: ID!, $limit: Int!)" in expanded.exec_source
    assert [v.node.variable.name.value for v in expanded.fragment_vars] == ["limit"]


SCHEMA_WITH_LIST_ARG = _schema("byIds(ids: [ID!]!): [String!]!")


def test_fragment_variable_list_type_synthesized():
    # Not from the brief's listed cases: pins that a list-shaped usage type
    # synthesizes a `ListTypeNode`, not just the scalar/named-type shapes the
    # other tests exercise.
    schema = graphql.build_schema(SCHEMA_WITH_LIST_ARG)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment(
        "fragment ImageParts on ImageAttachment { byIds(ids: $ids) }"
    )

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts},
        location="test:1",
    )

    assert "$ids: [ID!]!" in expanded.exec_source


def test_unknown_slot_kwarg_lists_slots():
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { url }")

    with pytest.raises(GraphQLGenerationError, match="attachment"):
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"wrongName": (image_parts,)},
            all_fragments={"ImageParts": image_parts},
            location="test:1",
        )


def test_incompatible_fragment_rejected():
    # `Post` shares no possible type with the `Attachment` union the
    # `attachment` slot resolves to.
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    post_fields = _fragment("fragment PostFields on Post { id }")

    with pytest.raises(GraphQLGenerationError, match="cannot be spread into slot"):
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (post_fields,)},
            all_fragments={"PostFields": post_fields},
            location="test:1",
        )


TWO_SLOT_TEMPLATE = """
query GetTwo($id: ID!) {
    post(id: $id) {
        id
        attachment @slot { __typename }
        preview: attachment @slot { __typename }
    }
}
"""

TWO_SLOTS = {
    "attachment": SlotTarget(type_name="Attachment", response_key="attachment"),
    "preview": SlotTarget(type_name="Attachment", response_key="preview"),
}


def test_every_bad_slot_argument_of_one_bind_is_reported_together():
    # The bind's own arguments are one diagnosis phase: a fragment that fits
    # no slot says nothing about the next slot, so stopping at the first one
    # would cost a regeneration per broken slot.
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TWO_SLOT_TEMPLATE)
    post_fields = _fragment("fragment PostFields on Post { id }")

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetTwo",
            slots=TWO_SLOTS,
            spreads={"attachment": (post_fields,), "preview": (post_fields,)},
            all_fragments={"PostFields": post_fields},
            location="test:1",
        )

    assert len(exc_info.value.errors) == 2
    assert all("cannot be spread into" in error for error in exc_info.value.errors)
    assert any("slot 'attachment'" in error for error in exc_info.value.errors)
    assert any("slot 'preview'" in error for error in exc_info.value.errors)


def test_an_unknown_slot_does_not_hide_another_slots_bad_fragment():
    # An unknown keyword has no slot type to check its own fragments against,
    # but the slots that do exist are still checked.
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TWO_SLOT_TEMPLATE)
    post_fields = _fragment("fragment PostFields on Post { id }")

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetTwo",
            slots=TWO_SLOTS,
            spreads={"attachment": (post_fields,), "wrongName": (post_fields,)},
            all_fragments={"PostFields": post_fields},
            location="test:1",
        )

    assert len(exc_info.value.errors) == 2
    assert any("slot 'attachment'" in error for error in exc_info.value.errors)
    assert any("unknown slot 'wrongName'" in error for error in exc_info.value.errors)


def test_a_slot_the_bind_leaves_empty_still_gets_a_readable_entry():
    # `readable_fragments` is keyed by the *template's* slots, not by the
    # bind's keywords: `collect._binding_slot` walks every slot of the template
    # and indexes this map, so an unfilled slot has to answer with an empty
    # entry instead of an absent key -- which would make "filled with nothing"
    # and "keyed by another name" one answer.
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TWO_SLOT_TEMPLATE)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { url }")

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetTwo",
        slots=TWO_SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts},
        location="test:1",
    )

    assert expanded.readable_fragments["preview"] == ()
    assert _direct(expanded) == {"attachment": ("ImageParts",), "preview": ()}
    # The empty slot is spliced with nothing, so the one spread the bind did
    # name lands at the one slot it named.
    assert expanded.exec_source.count("...ImageParts") == 1


def test_multiple_non_overlapping_fragments_share_one_slot():
    # Not from the brief's listed cases: the twin of the overlapping-coverage
    # case below — pins that a tuple of fragments on genuinely disjoint types
    # merges fine too, instead of only ever exercising the overlapping shape.
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { url }")
    link_parts = _fragment("fragment LinkParts on LinkAttachment { href }")

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts, link_parts)},
        all_fragments={"ImageParts": image_parts, "LinkParts": link_parts},
        location="test:1",
    )

    assert _direct(expanded) == {"attachment": ("ImageParts", "LinkParts")}
    assert "...ImageParts" in expanded.exec_source
    assert "...LinkParts" in expanded.exec_source


def test_overlapping_coverage_in_one_slot_is_accepted():
    # UrlParts and CaptionParts both cover ImageAttachment: each fragment
    # reads its own slice of the payload independently, so a bind naming both
    # expands cleanly.
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    url_parts = _fragment("fragment UrlParts on ImageAttachment { url }")
    caption_parts = _fragment("fragment CaptionParts on ImageAttachment { caption }")

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (url_parts, caption_parts)},
        all_fragments={"UrlParts": url_parts, "CaptionParts": caption_parts},
        location="test:1",
    )

    assert _direct(expanded) == {"attachment": ("CaptionParts", "UrlParts")}
    assert "...UrlParts" in expanded.exec_source
    assert "...CaptionParts" in expanded.exec_source


SCHEMA_WITH_THUMBNAIL = _schema("thumbnail(size: Int!): String!")


def test_merge_conflict_across_fragments_reported():
    # Only `ThumbWrapper` is directly bound; `ThumbSmall` reaches the operation
    # transitively through `ThumbWrapper`'s own spread. Two directly-bound
    # fragments could conflict the same way (pinned at the full-pipeline level
    # by test_overlapping_fragments_with_conflicting_fields_are_rejected in
    # tests/test_bindings_runtime.py); this shape instead proves the conflict
    # is caught for a fragment reached only through another's spread, not
    # named in the bind itself -- the final `graphql.validate` pass covers
    # both.
    schema = graphql.build_schema(SCHEMA_WITH_THUMBNAIL)
    template_doc = graphql.parse(TEMPLATE)
    thumb_small = _fragment(
        "fragment ThumbSmall on ImageAttachment { thumbnail(size: 100) }"
    )
    thumb_wrapper = _fragment("""
        fragment ThumbWrapper on ImageAttachment {
            thumbnail(size: 200)
            ...ThumbSmall
        }
        """)

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (thumb_wrapper,)},
            all_fragments={
                "ThumbSmall": thumb_small,
                "ThumbWrapper": thumb_wrapper,
            },
            location="test:1",
        )
    message = str(exc_info.value)
    assert "test:1" in message
    assert "thumbnail" in message
    # The combination is labeled template × fragments,
    # so a reader knows which template produced the conflict without cross-
    # referencing the bind location against the source.
    assert "GetAttachment" in message
    assert "ThumbWrapper" in message


SCHEMA_WITH_TYPED_ARGS = _schema(
    "byCount(x: Int!): String!", "byName(x: String!): String!"
)


def test_variable_type_conflict_between_usages_rejected():
    # `InnerParts` is only reached through `OuterParts`'s own spread, so the
    # conflicting usages of `$x` live in two different fragments of the same
    # closure, exactly like the merge-conflict case above.
    schema = graphql.build_schema(SCHEMA_WITH_TYPED_ARGS)
    template_doc = graphql.parse(TEMPLATE)
    inner_parts = _fragment("fragment InnerParts on ImageAttachment { byName(x: $x) }")
    outer_parts = _fragment(
        "fragment OuterParts on ImageAttachment { byCount(x: $x) ...InnerParts }"
    )

    # `match=r"\$x"` alone doesn't discriminate this
    # message from graphql-core's own -- deleting the whole check would keep
    # this test green, since graphql-core's error for the resulting document
    # also mentions `$x`. Pinned on text only this check's own message
    # produces.
    with pytest.raises(
        GraphQLGenerationError,
        match="no GraphQL variable declaration type is allowed at every usage",
    ):
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (outer_parts,)},
            all_fragments={
                "InnerParts": inner_parts,
                "OuterParts": outer_parts,
            },
            location="test:1",
        )


@pytest.mark.parametrize(
    ("conflicting_type", "expected_type"),
    [("String!", "'String!'"), ("[String]!", "'[String]!'")],
)
def test_variable_type_conflict_names_the_incompatible_usage(
    conflicting_type: str, expected_type: str
):
    schema = graphql.build_schema(
        _schema(
            "byCount(x: [Int]!): String!",
            "byOptional(x: [Int]): String!",
            f"byName(x: {conflicting_type}): String!",
        )
    )
    template_doc = graphql.parse(TEMPLATE)
    parts = _fragment(
        """
        fragment Parts on ImageAttachment {
            byCount(x: $x)
            byOptional(x: $x)
            byName(x: $x)
        }
        """
    )

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (parts,)},
            all_fragments={"Parts": parts},
            location="test:1",
        )

    message = str(exc_info.value)
    assert "'[Int]!'" in message
    assert expected_type in message
    assert "'[Int]' in fragment" not in message


SCHEMA_WITH_TWO_TYPED_ARGS = _schema(
    "byCount(x: Int!): String!",
    "byName(x: String!): String!",
    "yByCount(y: Int!): String!",
    "yByName(y: String!): String!",
)


def test_every_conflicting_variable_of_one_bind_is_reported_together():
    # `$x` disagreeing with itself says nothing about `$y`; both belong to the
    # same phase, so one regeneration names both.
    schema = graphql.build_schema(SCHEMA_WITH_TWO_TYPED_ARGS)
    template_doc = graphql.parse(TEMPLATE)
    inner_parts = _fragment(
        "fragment InnerParts on ImageAttachment { byName(x: $x) yByName(y: $y) }"
    )
    outer_parts = _fragment("""
        fragment OuterParts on ImageAttachment {
            byCount(x: $x)
            yByCount(y: $y)
            ...InnerParts
        }
        """)

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (outer_parts,)},
            all_fragments={"InnerParts": inner_parts, "OuterParts": outer_parts},
            location="test:1",
        )

    assert len(exc_info.value.errors) == 2
    assert all(
        "no GraphQL variable declaration type is allowed at every usage" in error
        for error in exc_info.value.errors
    )
    assert any("$x" in error for error in exc_info.value.errors)
    assert any("$y" in error for error in exc_info.value.errors)


def test_readable_and_variable_diagnoses_of_one_bind_arrive_together():
    # Both are answers about the same resolved closure -- what is readable at
    # the slot root, and what the closure's variables mean -- and they read
    # disjoint parts of it, so neither hides the other.
    schema = graphql.build_schema(SCHEMA_WITH_TYPED_ARGS)
    template_doc = graphql.parse(TEMPLATE)
    alt = _fragment("fragment Alt on ImageAttachment { url }")
    inner_parts = _fragment("fragment InnerParts on ImageAttachment { byName(x: $x) }")
    outer_parts = _fragment("""
        fragment OuterParts on ImageAttachment {
            byCount(x: $x)
            ...InnerParts
            ...Alt @include(if: $withAlt)
        }
        """)

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (outer_parts,)},
            all_fragments={
                "Alt": alt,
                "InnerParts": inner_parts,
                "OuterParts": outer_parts,
            },
            location="test:1",
        )

    assert len(exc_info.value.errors) == 2
    assert any("under @skip/@include" in error for error in exc_info.value.errors)
    assert any(
        "no GraphQL variable declaration type is allowed at every usage" in error
        for error in exc_info.value.errors
    )


def test_variable_used_by_two_fragments_at_agreeing_type_merges_into_one_declaration():
    # The intentional twin of the conflict test above: two *different*
    # fragments sharing a variable name at the same type is a merge, not a
    # collision (only a genuine type disagreement, or a
    # name shared with the template itself -- see the template-collision
    # test below). `InnerParts` is only reached through `OuterParts`'s own
    # spread, same closure shape as the conflict test, but both usages of
    # `$x` agree on `Int!` this time.
    schema = graphql.build_schema(SCHEMA_WITH_TYPED_ARGS)
    template_doc = graphql.parse(TEMPLATE)
    inner_parts = _fragment("fragment InnerParts on ImageAttachment { byCount(x: $x) }")
    outer_parts = _fragment(
        "fragment OuterParts on ImageAttachment { byCount(x: $x) ...InnerParts }"
    )

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (outer_parts,)},
        all_fragments={
            "InnerParts": inner_parts,
            "OuterParts": outer_parts,
        },
        location="test:1",
    )

    assert [v.node.variable.name.value for v in expanded.fragment_vars] == ["x"]
    assert expanded.exec_source.count("$x: Int!") == 1


def test_variable_uses_choose_one_graphql_compatible_declaration():
    schema = graphql.build_schema(
        _schema("byRequired(x: Int!): String!", "byOptional(x: Int): String!")
    )
    template_doc = graphql.parse(TEMPLATE)
    inner_parts = _fragment(
        "fragment InnerParts on ImageAttachment { byOptional(x: $x) }"
    )
    outer_parts = _fragment(
        "fragment OuterParts on ImageAttachment { byRequired(x: $x) ...InnerParts }"
    )

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (outer_parts,)},
        all_fragments={
            "InnerParts": inner_parts,
            "OuterParts": outer_parts,
        },
        location="test:1",
    )

    assert expanded.exec_source.count("$x: Int!") == 1
    assert graphql.validate(schema, graphql.parse(expanded.exec_source)) == []


def test_nested_list_usages_choose_one_graphql_compatible_declaration():
    schema = graphql.build_schema(
        _schema(
            "byOuterRequired(x: [Int]!): String!",
            "byInnerRequired(x: [Int!]): String!",
        )
    )
    template_doc = graphql.parse(TEMPLATE)
    inner_parts = _fragment(
        "fragment InnerParts on ImageAttachment { byInnerRequired(x: $x) }"
    )
    outer_parts = _fragment(
        """
        fragment OuterParts on ImageAttachment {
            byOuterRequired(x: $x)
            ...InnerParts
        }
        """
    )

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (outer_parts,)},
        all_fragments={
            "InnerParts": inner_parts,
            "OuterParts": outer_parts,
        },
        location="test:1",
    )

    assert expanded.exec_source.count("$x: [Int!]!") == 1
    assert graphql.validate(schema, graphql.parse(expanded.exec_source)) == []


def test_two_directly_bound_fragments_sharing_a_variable_are_rejected():
    # Finding 2 of the parametric-bind final review, and the design's own
    # example (§4): `ImageUrl` and `ImageAlt` are *siblings* here, each named
    # directly at its own slot -- neither is reached through the other's
    # spread, unlike the merge test above. Each is applied independently
    # through its own `with_args`, so agreeing on `$width`'s type (both
    # `Int!`) is not enough: the values are independent (`bind(left=image_url.
    # with_args(width=100), right=image_alt.with_args(width=200))`), and
    # `runtime.GQLBoundOperation.bound__` merges every direct fragment's
    # `fragment_args__` flat across the binding -- one of the two silently
    # wins.
    schema = graphql.build_schema("""
        type Query {
            post(id: ID!): Post
        }

        type Post {
            id: ID!
            left: ImageAttachment
            right: ImageAttachment
        }

        type ImageAttachment {
            url(width: Int!): String!
            alt(width: Int!): String!
        }
    """)
    template_doc = graphql.parse("""
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                left @slot { __typename }
                right @slot { __typename }
            }
        }
    """)
    two_slots = {
        "left": SlotTarget(type_name="ImageAttachment", response_key="left"),
        "right": SlotTarget(type_name="ImageAttachment", response_key="right"),
    }
    image_url = _fragment("fragment ImageUrl on ImageAttachment { url(width: $width) }")
    image_alt = _fragment("fragment ImageAlt on ImageAttachment { alt(width: $width) }")

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=two_slots,
            spreads={"left": (image_url,), "right": (image_alt,)},
            all_fragments={"ImageUrl": image_url, "ImageAlt": image_alt},
            location="test:1",
        )

    message = str(exc_info.value)
    assert "test:1" in message
    assert "$width" in message
    assert "ImageUrl" in message
    assert "ImageAlt" in message
    assert "rename" in message


SCHEMA_WITH_ID_ARG = _schema("byId(id: ID!): String!")


def test_variable_name_collision_with_template_var_is_rejected():
    # TEMPLATE declares `$id: ID!` for `post(id: $id)`.
    # ImageParts, defined in a different module by a different owner who
    # cannot see the template's own variable names, happens to also use
    # `$id` -- for something entirely unrelated (`byId`'s argument). Silently
    # letting the template's declaration win would ship `byId(id: $id)`
    # wired to whatever the template's `$id` means, with no `with_args` and
    # no way for the fragment's owner to notice.
    schema = graphql.build_schema(SCHEMA_WITH_ID_ARG)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { byId(id: $id) }")

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (image_parts,)},
            all_fragments={"ImageParts": image_parts},
            location="test:1",
        )

    message = str(exc_info.value)
    assert "test:1" in message
    assert "$id" in message
    assert "ImageParts" in message
    assert "GetAttachment" in message
    assert "rename" in message


SCHEMA_WITH_DEFAULT_ARG = _schema(
    "photos(limit: Int! = 5): [String!]!",
    "morePhotos(limit: Int!): [String!]!",
    "thumbnail(size: Int = 64): String!",
)


def test_fragment_variable_with_location_default_is_optional():
    # Not from the brief's listed cases: pins the algorithm's explicit
    # "required vs optional" carve-out — a
    # non-null usage backed by a schema-declared argument default is safe to
    # omit at runtime (the argument's own default applies), so the
    # synthesized declaration relaxes to nullable instead of forcing the
    # caller to supply it.
    schema = graphql.build_schema(SCHEMA_WITH_DEFAULT_ARG)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment(
        "fragment ImageParts on ImageAttachment { photos(limit: $limit) }"
    )

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts},
        location="test:1",
    )

    [synthesized] = expanded.fragment_vars
    assert synthesized.node.variable.name.value == "limit"
    assert isinstance(synthesized.node.type, graphql.NamedTypeNode)
    assert synthesized.node.type.name.value == "Int"
    assert isinstance(synthesized, OmittableSynthesizedVar)
    assert isinstance(synthesized.explicit_value_type, graphql.GraphQLNonNull)


def test_fragment_variable_at_a_nullable_position_with_a_default_is_optional():
    # A nullable position needs no relaxation, so "was the declared type
    # relaxed" is not the same question as "may the caller leave this out".
    # Answering the second with the first left `$size` a required keyword
    # whose only possible value crossed the wire as an explicit null -- and a
    # null is not an absent variable, so the schema's `= 64` became
    # unreachable through the generated API, against what the README promises
    # for every position the schema gives a default to.
    schema = graphql.build_schema(SCHEMA_WITH_DEFAULT_ARG)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment(
        "fragment ImageParts on ImageAttachment { thumbnail(size: $size) }"
    )

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts},
        location="test:1",
    )

    [synthesized] = expanded.fragment_vars
    assert synthesized.node.variable.name.value == "size"
    assert isinstance(synthesized.node.type, graphql.NamedTypeNode)
    assert isinstance(synthesized, OmittableSynthesizedVar)
    assert not isinstance(synthesized.explicit_value_type, graphql.GraphQLNonNull)


def test_fragment_variable_partial_location_default_stays_required():
    # `VariablesInAllowedPosition` is per-position —
    # a nullable variable is only allowed at a non-null position if *that*
    # position itself has a default. `photos` has one, `morePhotos` does not;
    # a nullable `$limit` would be rejected at `morePhotos`, so the
    # synthesized declaration must stay non-null even though one of the two
    # positions would have tolerated nullable on its own.
    schema = graphql.build_schema(SCHEMA_WITH_DEFAULT_ARG)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment("""
        fragment ImageParts on ImageAttachment {
            photos(limit: $limit)
            morePhotos(limit: $limit)
        }
        """)

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts},
        location="test:1",
    )

    [synthesized] = expanded.fragment_vars
    assert synthesized.node.variable.name.value == "limit"
    assert isinstance(synthesized.node.type, graphql.NonNullTypeNode)
    assert isinstance(synthesized, RequiredSynthesizedVar)
    assert isinstance(synthesized.explicit_value_type, graphql.GraphQLNonNull)
    assert "$limit: Int!" in expanded.exec_source


def test_fragment_variable_on_unknown_argument_defers_to_validate():
    # Not from the brief's listed cases: pins that a variable used at a
    # position `TypeInfo` cannot resolve an input type for (an argument name
    # the field doesn't declare) is not recorded as a usage — it is left
    # undeclared, and the final `graphql.validate` pass is the one place that
    # reports it, instead of a second, duplicate diagnosis here.
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment(
        "fragment ImageParts on ImageAttachment { url @include(bogus: $x) }"
    )

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (image_parts,)},
            all_fragments={"ImageParts": image_parts},
            location="test:1",
        )
    # Proves the error genuinely comes from
    # `graphql.validate` (both the unknown argument itself and the variable
    # it silently left undeclared), not a second diagnosis invented here.
    message = str(exc_info.value)
    assert "Unknown argument 'bogus'" in message
    assert "Variable '$x' is not defined" in message


TEMPLATE_WITH_LOCAL_FRAGMENT = """
fragment Extra on Post {
    id
}

query GetAttachment($id: ID!) {
    post(id: $id) {
        ...Extra
        attachment @slot { __typename }
    }
}
"""


def test_template_local_fragment_definition_is_preserved():
    # Not from the brief's listed cases: an operation statement carrying a
    # local fragment definition alongside itself is an established pattern
    # in this codebase (see test_slots.py's shadowing tests) — a template
    # statement is exactly such an operation statement, so its own local
    # definitions must survive expansion unchanged and undoubled.
    schema = graphql.build_schema(SCHEMA)
    template_doc = graphql.parse(TEMPLATE_WITH_LOCAL_FRAGMENT)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { url }")

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts},
        location="test:1",
    )

    assert expanded.exec_source.count("fragment Extra on Post") == 1
    assert "...Extra" in expanded.exec_source
    assert "...ImageParts" in expanded.exec_source


# --- What each slot can be read with, and what it cannot ---------------------

READABLE_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    hero: ImageAttachment
    attachment: Attachment
}

interface Node {
    id: ID!
}

type ImageAttachment implements Node {
    id: ID!
    url: String!
    caption: String!
    thumb: Thumb!
    sized(size: Int!): String!
}

type Thumb {
    alt: String!
}

# Also a Node, so a brick on `Node` can reach both members of the union and
# the per-type paths to it can differ in more than which fragment spreads it.
type LinkAttachment implements Node {
    id: ID!
    href: String!
}

# A third Node implementation, outside the `Attachment` union: an `... on
# VideoAttachment` branch is valid inside a fragment on Node, yet reaches no
# typename a slot of type ImageAttachment can hold.
type VideoAttachment implements Node {
    id: ID!
    duration: Int!
}

union Attachment = ImageAttachment | LinkAttachment
"""

NODE_ID = "fragment NodeId on Node { id }"
VIDEO_PARTS = "fragment VideoParts on VideoAttachment { duration }"
LINK_PARTS = "fragment LinkParts on LinkAttachment { href }"
THUMB_ALT = "fragment ThumbAlt on Thumb { alt }"


def _readable(expanded: ExpandedBinding) -> dict[str, dict[str, frozenset[str]]]:
    return {
        key: {entry.name: entry.typenames for entry in entries}
        for key, entries in expanded.readable_fragments.items()
    }


def test_a_brick_spread_under_a_narrower_condition_is_readable_only_there():
    # `NodeId` covers every Node, but this slot only ever reaches it through
    # `ImageParts`. Offering it at its own type condition made a perfectly
    # correct `LinkAttachment` payload -- which carries no `id`, because
    # `...NodeId` sits under the ImageAttachment branch -- fail validation.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { url ...NodeId }")
    link_parts = _fragment(LINK_PARTS)
    node_id = _fragment(NODE_ID)

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts, link_parts)},
        all_fragments={
            "ImageParts": image_parts,
            "LinkParts": link_parts,
            "NodeId": node_id,
        },
        location="test:1",
    )

    assert _readable(expanded) == {
        "attachment": {
            "ImageParts": frozenset({"ImageAttachment"}),
            "LinkParts": frozenset({"LinkAttachment"}),
            "NodeId": frozenset({"ImageAttachment"}),
        }
    }


def test_a_fragment_spread_inside_a_field_is_not_readable_at_the_slot_root():
    # `ThumbAlt`'s fields land under `thumb`, not on the slot's root payload.
    # Offering it as a readable definition validated it against the root, where `alt`
    # was never requested, so every response failed. Its definition still has
    # to reach the document, and its data still reaches the reader through
    # `ImageParts`'s own model.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment(
        "fragment ImageParts on ImageAttachment { url thumb { ...ThumbAlt } }"
    )
    thumb_alt = _fragment(THUMB_ALT)

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts, "ThumbAlt": thumb_alt},
        location="test:1",
    )

    assert _readable(expanded) == {
        "attachment": {"ImageParts": frozenset({"ImageAttachment"})}
    }
    assert "fragment ThumbAlt on Thumb" in expanded.exec_source


@pytest.mark.parametrize(
    "spread",
    [
        "...NodeId @include(if: $withId)",
        "... @include(if: $withId) { ...NodeId }",
        "... on ImageAttachment @skip(if: $noId) { ...NodeId }",
    ],
)
def test_a_conditional_root_level_path_to_a_brick_is_rejected(spread: str):
    # A readable fragment is validated on every response, so it cannot be
    # conditionally absent. Looking for the directive on the spread node alone
    # let an inline fragment carrying the same directive slip past, and the
    # generated binding then failed on a correct response.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment(
        f"fragment ImageParts on ImageAttachment {{ url {spread} }}"
    )
    node_id = _fragment(NODE_ID)

    with pytest.raises(
        GraphQLGenerationError,
        match=r"fragment 'ImageParts' spreads 'NodeId' under @skip/@include",
    ):
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (image_parts,)},
            all_fragments={"ImageParts": image_parts, "NodeId": node_id},
            location="test:1",
        )


def test_a_conditional_spread_is_allowed_when_the_brick_is_also_bound_directly():
    # The rule above is about a fragment that reaches the slot root *only*
    # under a directive. Bind the same brick directly and `_SlotFiller` writes
    # its spread at the slot root unconditionally, so its fields are always
    # requested and its definition is always safe to validate -- rejecting this
    # made a correct document ungeneratable, and both remedies the diagnosis
    # offered changed what the developer had asked for.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment(
        "fragment ImageParts on ImageAttachment { url ...NodeId @include(if: $withId) }"
    )
    node_id = _fragment(NODE_ID)

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts, node_id)},
        all_fragments={"ImageParts": image_parts, "NodeId": node_id},
        location="test:1",
    )

    readable = _readable(expanded)["attachment"]
    assert set(readable) == {"ImageParts", "NodeId"}
    # `NodeId` carries the typenames it is reached at unconditionally -- the
    # direct bind, cut to the slot's own types -- which here covers every
    # typename the conditional path could deliver it at, so that path
    # contributes nothing the definition could be wrong about.
    assert readable["NodeId"] == frozenset({"ImageAttachment", "LinkAttachment"})


def test_a_conditional_path_to_a_typename_reached_no_other_way_is_rejected():
    # The rule is per typename, not per fragment name. `NodeId` is reached
    # unconditionally on ImageAttachment and only under a directive on
    # LinkAttachment, so its definition covers ImageAttachment alone -- and a
    # LinkAttachment payload that *did* carry the fields (the condition was
    # true) read back as None. Asking whether the fragment is reachable at all,
    # rather than at which typenames, let exactly this combination through.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { url ...NodeId }")
    link_parts = _fragment(
        "fragment LinkParts on LinkAttachment { href ...NodeId @include(if: $withId) }"
    )
    node_id = _fragment(NODE_ID)

    with pytest.raises(
        GraphQLGenerationError,
        match=(
            r"fragment 'LinkParts' spreads 'NodeId' under @skip/@include, and "
            r"at LinkAttachment that is the only way"
        ),
    ):
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (image_parts, link_parts)},
            all_fragments={
                "ImageParts": image_parts,
                "LinkParts": link_parts,
                "NodeId": node_id,
            },
            location="test:1",
        )


def test_a_brick_reachable_at_no_typename_of_the_slot_is_not_offered():
    # `hero` is an ImageAttachment, so a Node fragment bound there narrows to
    # {ImageAttachment}; the `... on VideoAttachment` branch inside it can never
    # match, and `VideoParts` behind it is present on no payload this slot ever
    # returns. Registering it anyway produced a reader with an empty typename
    # set, which type-checks and returns None for every single response.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse("""
        query GetHero($id: ID!) {
            post(id: $id) {
                id
                hero @slot { __typename }
            }
        }
        """)
    node_id = _fragment("""
        fragment NodeId on Node {
            id
            ... on VideoAttachment { ...VideoParts }
        }
        """)
    video_parts = _fragment(VIDEO_PARTS)

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetHero",
        slots={"hero": SlotTarget(type_name="ImageAttachment", response_key="hero")},
        spreads={"hero": (node_id,)},
        all_fragments={"NodeId": node_id, "VideoParts": video_parts},
        location="test:1",
    )

    assert set(_readable(expanded)["hero"]) == {"NodeId"}


def test_a_conditional_spread_inside_a_field_is_allowed():
    # The mirror image of the rule above: nothing offers `ThumbAlt` as a
    # reader, and the fields it contributes to `ImageParts`'s own model are
    # collected under the condition, hence optional -- so there is nothing
    # here for the rejection to protect.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    image_parts = _fragment("""
        fragment ImageParts on ImageAttachment {
            url
            thumb { ...ThumbAlt @include(if: $withAlt) }
        }
        """)
    thumb_alt = _fragment(THUMB_ALT)

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts, "ThumbAlt": thumb_alt},
        location="test:1",
    )

    assert "$withAlt: Boolean!" in expanded.exec_source


TEMPLATE_SPREADING_A_BOUND_FRAGMENT = """
query GetAttachment($id: ID!, $size: Int!) {
    post(id: $id) {
        id
        hero { ...ImageParts }
        attachment @slot { __typename }
    }
}
"""


def test_a_bound_fragment_the_template_already_spreads_has_two_owners():
    # Static spread отдаёт $size методу execute, а binding ImageParts — методу
    # ImageParts.with_args. В GraphQL declaration одна, поэтому ни один источник
    # не может молча перекрыть другой.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE_SPREADING_A_BOUND_FRAGMENT)
    image_parts = _fragment(
        "fragment ImageParts on ImageAttachment { sized(size: $size) }"
    )
    template_doc = graphql.DocumentNode(
        definitions=[*template_doc.definitions, image_parts]
    )

    with pytest.raises(GraphQLGenerationError) as exc_info:
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (image_parts,)},
            all_fragments={"ImageParts": image_parts},
            location="test:1",
        )

    message = str(exc_info.value)
    assert "Fragment 'ImageParts'" in message
    assert "$size" in message
    assert "template 'GetAttachment'" in message


TEMPLATE_WITH_LOCAL_IMAGE_PARTS = """
fragment ImageParts on ImageAttachment {
    caption
}

query GetAttachment($id: ID!) {
    post(id: $id) {
        id
        hero { ...ImageParts }
        attachment @slot { __typename }
    }
}
"""


def test_a_local_definition_conflicting_with_a_bound_fragment_is_rejected():
    # Merging same-named definitions is what lets a template and a bind share
    # one brick, but it assumed the name always meant the same definition. A
    # template statement may define one locally, and silently keeping that
    # copy shipped a document whose `ImageParts` selects `caption` while the
    # definition's `ImagePartsData` requires `url` -- a correct response then
    # failed validation, with nothing at generation time to say why.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE_WITH_LOCAL_IMAGE_PARTS)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { url }")

    with pytest.raises(
        GraphQLGenerationError,
        match=r"carries fragment 'ImageParts' in its closure, but the template",
    ):
        expand_binding(
            schema=schema,
            template_doc=template_doc,
            template_operation=_operation(template_doc),
            template_name="GetAttachment",
            slots=SLOTS,
            spreads={"attachment": (image_parts,)},
            all_fragments={"ImageParts": image_parts},
            location="test:1",
        )


def test_an_identical_local_definition_merges_into_one():
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE_WITH_LOCAL_IMAGE_PARTS)
    image_parts = _fragment("fragment ImageParts on ImageAttachment { caption }")

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={"ImageParts": image_parts},
        location="test:1",
    )

    assert expanded.exec_source.count("fragment ImageParts on ImageAttachment") == 1


def test_a_brick_reached_by_two_paths_is_offered_once():
    # `NodeId` sits under both halves of the same bound fragment. Walking each
    # path independently would offer it twice -- and report any diagnosis
    # along it twice with it.
    schema = graphql.build_schema(READABLE_SCHEMA)
    template_doc = graphql.parse(TEMPLATE)
    left = _fragment("fragment Left on ImageAttachment { url ...NodeId }")
    right = _fragment("fragment Right on ImageAttachment { caption ...NodeId }")
    image_parts = _fragment(
        "fragment ImageParts on ImageAttachment { ...Left ...Right }"
    )
    node_id = _fragment(NODE_ID)

    expanded = expand_binding(
        schema=schema,
        template_doc=template_doc,
        template_operation=_operation(template_doc),
        template_name="GetAttachment",
        slots=SLOTS,
        spreads={"attachment": (image_parts,)},
        all_fragments={
            "ImageParts": image_parts,
            "Left": left,
            "Right": right,
            "NodeId": node_id,
        },
        location="test:1",
    )

    assert _readable(expanded) == {
        "attachment": {
            "ImageParts": frozenset({"ImageAttachment"}),
            "Left": frozenset({"ImageAttachment"}),
            "NodeId": frozenset({"ImageAttachment"}),
            "Right": frozenset({"ImageAttachment"}),
        }
    }
