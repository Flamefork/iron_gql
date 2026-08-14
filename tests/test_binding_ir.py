from pathlib import Path

import pytest
from pydantic import alias_generators

from iron_gql.codegen.collect import collect_package_ir
from iron_gql.codegen.discovery import BindDecl
from iron_gql.codegen.discovery import Statement
from iron_gql.codegen.ir import CollectedBinding
from iron_gql.codegen.ir import CollectedBindingSlot
from iron_gql.codegen.ir import CollectedFactoryFragment
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import CollectedRequiredFragmentArg
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ImportRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.parser import ParseResult
from iron_gql.codegen.parser import parse_gql_queries
from iron_gql.codegen.slots import validate_no_nested_slots
from iron_gql.slots import CombinationKey


def _binding(ir: CollectedPackageIR, key: CombinationKey) -> CollectedBinding:
    # One combination out of the package's enumeration. Every template is
    # enumerated in full now (the empty combination, then one per compatible
    # fragment per slot), so a test after a particular pair has to name its
    # key rather than unpack the only binding there is.
    [binding] = [binding for binding in ir.bindings if binding.combination_key == key]
    return binding


def _definition_classes(binding_slot: CollectedBindingSlot) -> tuple[str, ...]:
    return tuple(f.class_name for f in binding_slot.direct_fragments)


def _model_names(binding_slot: CollectedBindingSlot) -> tuple[str, ...]:
    return tuple(f.model_name for f in binding_slot.direct_fragments)


SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
    photos(limit: Int!): [String!]!
}

type LinkAttachment {
    href: String!
}
"""

TEMPLATE_TEXT = """
query GetAttachment($id: ID!) {
    post(id: $id) {
        id
        attachment @slot { __typename }
    }
}
"""

FRAGMENT_TEXT = """
fragment ImageParts on ImageAttachment {
    photos(limit: $limit)
}
"""

# A second, unrelated single-fragment statement on the same slot type: used
# wherever a test needs two bindings of the same template that must stay two
# combinations, and so two classes.
OTHER_FRAGMENT_TEXT = """
fragment ImageUrl on ImageAttachment {
    url
}
"""

PLAIN_OPERATION_TEXT = 'query Plain { post(id: "1") { id } }'

SCALARS = {"ID": ImportRef.parse("builtins:str")}

# A schema where the only path to an input object type is a bound fragment's
# own (synthesized) variable -- no query in the package declares one itself.
INPUT_TYPE_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

input PhotoFilter {
    query: String
}

type ImageAttachment {
    url: String!
    photos(filter: PhotoFilter): [String!]!
}

type LinkAttachment {
    href: String!
}
"""

INPUT_TYPE_FRAGMENT_TEXT = """
fragment ImageParts on ImageAttachment {
    photos(filter: $filter)
}
"""

# `Comment` не является `Attachment`, не spread-compatible с type slot
# `attachment` и вообще недостижим ни из одного slot пакета. Фрагмент на нём —
# factory, которую не bind-ит ни один combination: это случай «не совместим ни
# с одним slot» из контракта `bindings.fragment_closure`.
ORPHAN_INPUT_TYPE_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}

type Comment {
    id: ID!
    body(filter: CommentFilter): String!
}

input CommentFilter {
    tone: String
}
"""

ORPHAN_FRAGMENT_TEXT = """
fragment OrphanBits on Comment {
    body(filter: $filter)
}
"""

# A template with two independent slots, for the "one bind, one slot left
# unfilled" scenario -- `FRAGMENT_TEXT`'s own `ImageAttachment` shape
# (`photos(limit: ...)`) isn't needed here, so this uses a plain `url` field.
TWO_SLOT_SCHEMA = """
type Query {
    post(id: ID!): Post
}

type Post {
    id: ID!
    attachment: Attachment
    preview: Attachment
}

union Attachment = ImageAttachment | LinkAttachment

type ImageAttachment {
    url: String!
}

type LinkAttachment {
    href: String!
}
"""

TWO_SLOT_TEMPLATE_TEXT = """
query GetAttachment($id: ID!) {
    post(id: $id) {
        id
        attachment @slot { __typename }
        preview @slot { __typename }
    }
}
"""

TWO_SLOT_FRAGMENT_TEXT = """
fragment ImageParts on ImageAttachment {
    url
}
"""


def _stmt(text: str, name: str) -> Statement:
    return Statement(raw_text=text, file=Path(f"<test:{name}>"), lineno=1)


def _write_schema(tmp_path: Path) -> Path:
    path = tmp_path / "schema.graphql"
    path.write_text(SCHEMA, encoding="utf-8")
    return path


def _collect(parse_res: ParseResult, binds: list[BindDecl]) -> CollectedPackageIR:
    return collect_package_ir(
        schema=parse_res.schema,
        operations=parse_res.operations,
        templates=parse_res.templates,
        fragment_statements=parse_res.bindable_statements,
        binds=binds,
        bind_keyword_checks=(),
        discovered_texts=(),
        scalars=SCALARS,
        to_snake_fn=alias_generators.to_snake,
    )


def test_template_is_classified_not_an_operation(tmp_path: Path):
    schema_path = _write_schema(tmp_path)
    template_stmt = _stmt(TEMPLATE_TEXT, "template")
    fragment_stmt = _stmt(FRAGMENT_TEXT, "fragment")
    bind = BindDecl(
        template=template_stmt,
        slot_args=(("attachment", (fragment_stmt,)),),
        locations=("<test:bind>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, fragment_stmt],
        [bind],
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, [bind])

    assert [operation.class_name for operation in ir.operations] == []
    [template] = ir.templates
    assert template.class_name == "GetAttachment"
    assert template.bound_base_name == "GetAttachmentBound"
    assert template.result_type == "GetAttachmentResult"
    assert not template.is_subscription
    [slot] = template.slots
    assert slot.name == "attachment"
    assert slot.python_name == "attachment"
    # The generated slot node models this template's `attachment` field
    # collects into -- `_collect_typed_field`'s own naming. The slot's type is
    # a union, so the key reaches one node model per variant; the union alias
    # over them is not a node model itself and is reached from them through
    # the dependency graph.
    assert slot.node_types == (
        "GetAttachmentResultPostAttachmentSlotImageAttachment",
        "GetAttachmentResultPostAttachmentSlotLinkAttachment",
    )


def test_binding_collected_with_spread_model_names_and_arg_vars(tmp_path: Path):
    schema_path = _write_schema(tmp_path)
    template_stmt = _stmt(TEMPLATE_TEXT, "template")
    fragment_stmt = _stmt(FRAGMENT_TEXT, "fragment")
    bind = BindDecl(
        template=template_stmt,
        slot_args=(("attachment", (fragment_stmt,)),),
        locations=("<test:bind>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, fragment_stmt],
        [bind],
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, [bind])

    # The combination's identity is its key: the template, then the slot and
    # the fragments filling it. No call site had to supply a name for it --
    # and no call site is needed to produce it either, which is why the whole
    # enumeration is pinned here beside the one pair this test is about.
    assert [binding.combination_key for binding in ir.bindings] == [
        ("GetAttachment", ()),
        ("GetAttachment", (("attachment", ("ImageParts",)),)),
    ]
    binding = _binding(ir, ("GetAttachment", (("attachment", ("ImageParts",)),)))
    assert binding.template.class_name == "GetAttachment"
    # A combination somebody wrote answers with the line they wrote -- that is
    # the edit that removes it.
    assert binding.location == "<test:bind>:1"
    # One nobody wrote answers with the statements it is made of instead: the
    # schema produced it, so the template and its fragments are what a
    # developer edits to change it.
    empty = _binding(ir, ("GetAttachment", ()))
    assert empty.location == "<test:template>:1"
    assert "@slot" not in binding.exec_source
    assert "...ImageParts" in binding.exec_source
    assert "fragment ImageParts on ImageAttachment" in binding.exec_source

    [binding_slot] = binding.slots
    assert binding_slot.slot.name == "attachment"
    assert _definition_classes(binding_slot) == ("ImageParts",)
    assert _model_names(binding_slot) == ("ImagePartsData",)

    # The fragment itself becomes a typed definition because the package holds a
    # template at all -- no `.bind()` naming it is required.
    [fragment] = ir.fragments
    assert fragment.fragment_name == "ImageParts"
    # У `$limit` нет schema default, поэтому `ImageParts` — factory: её
    # параметр `with_args` хранится на фрагменте, а не на binding.
    assert isinstance(fragment, CollectedFactoryFragment)
    assert fragment.applied_class_name == "_ImagePartsApplied"
    assert fragment.binding_class_name == "_ImagePartsApplied"
    assert fragment.bound_closure[0] == "_ImagePartsApplied"
    [arg] = fragment.arg_vars
    assert isinstance(arg, CollectedRequiredFragmentArg)
    assert arg.gql_name == "limit"
    assert arg.python_name == "limit"
    assert arg.explicit_value_type == ScalarRef(expr="int", name_hint="Int")


# `ImageParts` spreads `BaseParts` -- only `ImageParts` is named in the bind.
COMPOSED_BASE_FRAGMENT_TEXT = """
fragment BaseParts on ImageAttachment {
    url
}
"""

COMPOSED_FRAGMENT_TEXT = """
fragment ImageParts on ImageAttachment {
    ...BaseParts
}
"""


def test_readable_fragments_widen_but_direct_fields_stay_scoped(
    tmp_path: Path,
):
    # The template binding specs (rendered from `readable_fragments`) must offer
    # every fragment readable at the slot's root so it reads independently,
    # but `direct_fragments` -- what drives `bind()`'s overload
    # shapes and the runtime binding key -- must stay scoped to exactly what
    # the caller passed to `bind()`, unaffected by that widening.
    schema_path = _write_schema(tmp_path)
    template_stmt = _stmt(TEMPLATE_TEXT, "template")
    base_stmt = _stmt(COMPOSED_BASE_FRAGMENT_TEXT, "base_fragment")
    fragment_stmt = _stmt(COMPOSED_FRAGMENT_TEXT, "fragment")
    bind = BindDecl(
        template=template_stmt,
        slot_args=(("attachment", (fragment_stmt,)),),
        locations=("<test:bind>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, base_stmt, fragment_stmt],
        [bind],
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, [bind])

    binding = _binding(ir, ("GetAttachment", (("attachment", ("ImageParts",)),)))
    [binding_slot] = binding.slots
    assert _definition_classes(binding_slot) == ("ImageParts",)
    assert _model_names(binding_slot) == ("ImagePartsData",)
    assert [
        (readable.fragment.class_name, readable.typenames)
        for readable in binding_slot.readable_fragments
    ] == [
        ("BaseParts", ("ImageAttachment",)),
        ("ImageParts", ("ImageAttachment",)),
    ]


# A second fragment on the *other* member of the slot's union, named so that
# alphabetical order is the reverse of the order a bind can list them in.
LINK_FRAGMENT_TEXT = """
fragment LinkHref on LinkAttachment {
    href
}
"""


def test_direct_fragments_are_ordered_by_fragment_name_not_by_call_order(
    tmp_path: Path,
):
    # `direct_fragments` is the set the bind named, in the canonical order of
    # the fragment names -- the walk that produces it has no call position to
    # keep (see its comment in ir.py). Pinned because it is not invisible: the
    # rendered `bind()` overload of a multi-fragment slot spells this order out
    # as its parameter union.
    schema_path = _write_schema(tmp_path)
    template_stmt = _stmt(TEMPLATE_TEXT, "template")
    image_stmt = _stmt(OTHER_FRAGMENT_TEXT, "image_fragment")
    link_stmt = _stmt(LINK_FRAGMENT_TEXT, "link_fragment")
    bind = BindDecl(
        template=template_stmt,
        # Listed against the alphabetical order on purpose.
        slot_args=(("attachment", (link_stmt, image_stmt)),),
        locations=("<test:bind>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, image_stmt, link_stmt],
        [bind],
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, [bind])

    binding = _binding(
        ir, ("GetAttachment", (("attachment", ("ImageUrl", "LinkHref")),))
    )
    [binding_slot] = binding.slots
    assert tuple(f.fragment_name for f in binding_slot.direct_fragments) == (
        "ImageUrl",
        "LinkHref",
    )


def test_a_fragment_discovered_in_two_places_keeps_both_locations(tmp_path: Path):
    # The same contract an operation's `locations` carries: dedup keeps one
    # statement per name, and a diagnosis quoting only the first sends the
    # developer to a file that may not be the one they have to edit.
    schema_path = _write_schema(tmp_path)
    template_stmt = _stmt(TEMPLATE_TEXT, "template")
    fragment_stmt = _stmt(FRAGMENT_TEXT, "fragment")
    copy_stmt = _stmt(FRAGMENT_TEXT, "fragment_copy")
    bind = BindDecl(
        template=template_stmt,
        slot_args=(("attachment", (fragment_stmt,)),),
        locations=("<test:bind>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, fragment_stmt, copy_stmt],
        [bind],
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, [bind])

    [fragment] = ir.fragments
    assert fragment.locations == ("<test:fragment>:1", "<test:fragment_copy>:1")
    assert fragment.location == "<test:fragment>:1, <test:fragment_copy>:1"


def test_bind_template_ref_with_no_slots_is_rejected(tmp_path: Path):
    schema_path = _write_schema(tmp_path)
    plain_stmt = _stmt(PLAIN_OPERATION_TEXT, "plain")
    bind = BindDecl(
        template=plain_stmt,
        slot_args=(),
        locations=("<test:bad>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path, [plain_stmt], [bind], bind_keyword_checks=()
    )

    assert any("has no slots" in error for error in parse_res.errors)


def test_bind_template_ref_to_a_fragment_is_rejected(tmp_path: Path):
    schema_path = _write_schema(tmp_path)
    fragment_only_stmt = _stmt(FRAGMENT_TEXT, "fragment_only")
    bind = BindDecl(
        template=fragment_only_stmt,
        slot_args=(),
        locations=("<test:bad>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path, [fragment_only_stmt], [bind], bind_keyword_checks=()
    )

    assert any("is not an operation" in error for error in parse_res.errors)


def test_bind_template_ref_to_an_invalid_operation_is_diagnosed_accurately(
    tmp_path: Path,
):
    # The statement IS an operation (unlike the fragment case above) -- it
    # just fails GraphQL validation on its own terms. "is not an operation"
    # would be an inaccurate diagnosis for that; the real error is already
    # reported separately as a GraphQL error for the same statement.
    schema_path = _write_schema(tmp_path)
    broken_stmt = _stmt('query Broken { post(id: "1") { nonExistentField } }', "broken")
    bind = BindDecl(
        template=broken_stmt,
        slot_args=(),
        locations=("<test:bad>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path, [broken_stmt], [bind], bind_keyword_checks=()
    )

    assert not any("is not an operation" in error for error in parse_res.errors)
    assert any("failed to validate" in error for error in parse_res.errors)
    assert any("nonExistentField" in error for error in parse_res.errors)


def test_bind_slot_arg_not_a_single_fragment_statement_is_rejected(tmp_path: Path):
    schema_path = _write_schema(tmp_path)
    template_stmt = _stmt(TEMPLATE_TEXT, "template")
    plain_stmt = _stmt(PLAIN_OPERATION_TEXT, "plain")
    bind = BindDecl(
        template=template_stmt,
        slot_args=(("attachment", (plain_stmt,)),),
        locations=("<test:bad>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, plain_stmt],
        [bind],
        bind_keyword_checks=(),
    )

    assert any(
        "is not a single-fragment statement" in error for error in parse_res.errors
    )


def test_a_fragment_in_two_slots_gives_two_combination_keys(tmp_path: Path):
    # Slot входит в логическую идентичность: один fragment в двух slots одного
    # template образует две разные combinations.
    schema_path = tmp_path / "schema.graphql"
    schema_path.write_text(TWO_SLOT_SCHEMA, encoding="utf-8")
    template_stmt = _stmt(TWO_SLOT_TEMPLATE_TEXT, "template")
    fragment_stmt = _stmt(TWO_SLOT_FRAGMENT_TEXT, "fragment")
    on_attachment = BindDecl(
        template=template_stmt,
        slot_args=(("attachment", (fragment_stmt,)),),
        locations=("<test:on_attachment>:1",),
    )
    on_preview = BindDecl(
        template=template_stmt,
        slot_args=(("preview", (fragment_stmt,)),),
        locations=("<test:on_preview>:1",),
    )

    binds = [on_attachment, on_preview]
    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, fragment_stmt],
        binds,
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, binds)

    # The whole product, which is what the enumeration writes whether or not
    # a `.bind()` names any of it -- and inside it, the two combinations this
    # test is about: the same fragment in two different slots keeps two keys.
    assert [binding.combination_key for binding in ir.bindings] == [
        ("GetAttachment", ()),
        ("GetAttachment", (("preview", ("ImageParts",)),)),
        ("GetAttachment", (("attachment", ("ImageParts",)),)),
        (
            "GetAttachment",
            (("attachment", ("ImageParts",)), ("preview", ("ImageParts",))),
        ),
    ]


def test_binding_variable_of_input_object_type_is_collected(tmp_path: Path):
    # `$filter`'s type (PhotoFilter) is reachable only through this bind's own
    # synthesized variable -- no query in the package declares it directly --
    # so this pins that `collect_package_ir` feeds bindings' variable types
    # into the input-type closure, not just queries' own `variables`.
    schema_path = tmp_path / "schema.graphql"
    schema_path.write_text(INPUT_TYPE_SCHEMA, encoding="utf-8")
    template_stmt = _stmt(TEMPLATE_TEXT, "template")
    fragment_stmt = _stmt(INPUT_TYPE_FRAGMENT_TEXT, "fragment")
    bind = BindDecl(
        template=template_stmt,
        slot_args=(("attachment", (fragment_stmt,)),),
        locations=("<test:bind>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, fragment_stmt],
        [bind],
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, [bind])

    [fragment] = ir.fragments
    assert isinstance(fragment, CollectedFactoryFragment)
    [arg] = fragment.arg_vars
    assert isinstance(arg, CollectedRequiredFragmentArg)
    assert arg.gql_name == "filter"
    assert arg.explicit_value_type == NamedRef(name="PhotoFilter", nullable=True)

    input_names = {
        artifact.name
        for artifact in ir.input_artifacts
        if isinstance(artifact, CollectedModel)
    }
    assert "PhotoFilter" in input_names


def test_an_orphan_factorys_input_object_type_is_collected(tmp_path: Path):
    # `OrphanBits` is on `Comment`, spread-compatible with no slot in the
    # package (`bindings.fragment_closure`'s own "compatible with no slot in
    # the package" case) -- so no combination ever reaches it, and
    # `_collect_input_artifacts_with_binds`'s combination-level walk alone
    # would never see `$filter`'s type. It is still a factory -- its own
    # closure uses a variable -- and still renders a
    # `with_args(*, filter: CommentFilter | None)`, so `CommentFilter` has to
    # be collected regardless of whether any binding ever reaches it.
    schema_path = tmp_path / "schema.graphql"
    schema_path.write_text(ORPHAN_INPUT_TYPE_SCHEMA, encoding="utf-8")
    template_stmt = _stmt(TEMPLATE_TEXT, "template")
    orphan_stmt = _stmt(ORPHAN_FRAGMENT_TEXT, "orphan")

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, orphan_stmt],
        [],
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, [])

    [fragment] = ir.fragments
    assert fragment.fragment_name == "OrphanBits"
    assert isinstance(fragment, CollectedFactoryFragment)
    [arg] = fragment.arg_vars
    assert isinstance(arg, CollectedRequiredFragmentArg)
    assert arg.gql_name == "filter"
    assert arg.explicit_value_type == NamedRef(name="CommentFilter", nullable=True)

    input_names = {
        artifact.name
        for artifact in ir.input_artifacts
        if isinstance(artifact, CollectedModel)
    }
    assert "CommentFilter" in input_names


# An interface no type implements: legal SDL, and a query selecting a field of
# that type is a legal document -- graphql-core validates both -- so the
# collector is the first layer that can say anything about it.
NO_IMPLEMENTATION_SCHEMA = """
type Query {
    node: Node
}

interface Node {
    id: ID!
}
"""


def test_interface_with_no_implementing_type_is_diagnosed(tmp_path: Path):
    # A collector diagnosis, not an internal invariant: nothing before it
    # rejects this package, and there is no object type whose payload the
    # models could describe.
    schema_path = tmp_path / "schema.graphql"
    schema_path.write_text(NO_IMPLEMENTATION_SCHEMA, encoding="utf-8")
    query_stmt = _stmt("query GetNode { node { __typename id } }", "query")

    parse_res = parse_gql_queries(schema_path, [query_stmt], [], bind_keyword_checks=())
    assert parse_res.errors == []

    with pytest.raises(GraphQLGenerationError) as exc_info:
        _collect(parse_res, [])

    [error] = exc_info.value.errors
    assert "Interface 'Node' selected by field node in 'GetNode'" in error
    assert "has no implementing type in the schema" in error


def test_nested_slot_in_a_template_is_rejected(tmp_path: Path):
    # `validate_no_nested_slots` used to walk `ir.operations`, which can no
    # longer carry a slot at all now that a slotted operation is a template —
    # this pins that it walks `ir.templates` instead, and still catches a
    # slot nested inside another slot's own subtree.
    schema_path = _write_schema(tmp_path)
    nested_stmt = _stmt(
        """
        query GetPost($id: ID!) {
            post(id: $id) @slot {
                __typename
                id
                attachment @slot { __typename }
            }
        }
        """,
        "nested",
    )

    parse_res = parse_gql_queries(
        schema_path, [nested_stmt], [], bind_keyword_checks=()
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, [])

    assert ir.operations == []
    [template] = ir.templates
    assert template.class_name == "GetPost"

    errors = validate_no_nested_slots(ir)
    assert any("nested inside slot" in error for error in errors)


def test_statically_excluded_slot_in_a_template_is_rejected(tmp_path: Path):
    # A literal `@include(if: false)` drops the slot field from the collected
    # models, leaving a template whose slot promises fragment data that can
    # never arrive. Rejected by collection itself, where the absence is
    # known -- so `CollectedTemplateSlot.node_types` is non-empty by
    # construction and nothing downstream has to re-derive the exclusion.
    schema_path = _write_schema(tmp_path)
    excluded_stmt = _stmt(
        """
        query GetAttachment($id: ID!) {
            post(id: $id) {
                id
                ... @include(if: false) {
                    attachment @slot { __typename }
                }
            }
        }
        """,
        "excluded",
    )

    parse_res = parse_gql_queries(
        schema_path, [excluded_stmt], [], bind_keyword_checks=()
    )
    assert parse_res.errors == []

    with pytest.raises(GraphQLGenerationError) as exc_info:
        _collect(parse_res, [])
    assert any("statically excluded" in error for error in exc_info.value.errors)


def test_binding_leaves_an_unfilled_slot_empty(tmp_path: Path):
    # A template can declare more slots than one bind fills; the unfilled
    # one must still get a `CollectedBindingSlot`, just with nothing in it.
    schema_path = tmp_path / "schema.graphql"
    schema_path.write_text(TWO_SLOT_SCHEMA, encoding="utf-8")
    template_stmt = _stmt(TWO_SLOT_TEMPLATE_TEXT, "template")
    fragment_stmt = _stmt(TWO_SLOT_FRAGMENT_TEXT, "fragment")
    bind = BindDecl(
        template=template_stmt,
        # Only `attachment` is filled; `preview` is left out entirely.
        slot_args=(("attachment", (fragment_stmt,)),),
        locations=("<test:bind>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, fragment_stmt],
        [bind],
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    ir = _collect(parse_res, [bind])

    binding = _binding(ir, ("GetAttachment", (("attachment", ("ImageParts",)),)))
    slots_by_name = {
        binding_slot.slot.name: binding_slot for binding_slot in binding.slots
    }
    assert set(slots_by_name) == {"attachment", "preview"}
    assert _definition_classes(slots_by_name["attachment"]) == ("ImageParts",)
    assert _model_names(slots_by_name["attachment"]) == ("ImagePartsData",)
    assert _definition_classes(slots_by_name["preview"]) == ()
    assert _model_names(slots_by_name["preview"]) == ()


def test_multiple_broken_binds_are_reported_together(tmp_path: Path):
    # Binding expansion must accumulate like parser.py's
    # validate_bind_* family instead of stopping at the first broken bind.
    # Two independently broken binds, each failing inside `expand_binding`
    # for a different reason -- both diagnoses must come back from one
    # `collect_package_ir` call.
    schema_path = _write_schema(tmp_path)
    template_stmt = _stmt(TEMPLATE_TEXT, "template")
    fragment_stmt = _stmt(FRAGMENT_TEXT, "fragment")
    post_fields_stmt = _stmt("fragment PostFields on Post { id }", "post_fields")
    unknown_slot = BindDecl(
        template=template_stmt,
        slot_args=(("bogus", (fragment_stmt,)),),
        locations=("<test:unknown_slot>:1",),
    )
    incompatible = BindDecl(
        template=template_stmt,
        slot_args=(("attachment", (post_fields_stmt,)),),
        locations=("<test:incompatible>:1",),
    )

    parse_res = parse_gql_queries(
        schema_path,
        [template_stmt, fragment_stmt, post_fields_stmt],
        [unknown_slot, incompatible],
        bind_keyword_checks=(),
    )
    assert parse_res.errors == []

    with pytest.raises(GraphQLGenerationError) as exc_info:
        _collect(parse_res, [unknown_slot, incompatible])

    message = str(exc_info.value)
    # Each diagnosis points at the `.bind()` call it is about: these two
    # combinations exist because somebody wrote them, and the line they wrote
    # is the only edit that removes either one.
    assert "unknown slot" in message
    assert "<test:unknown_slot>:1" in message
    assert "cannot be spread into slot" in message
    assert "<test:incompatible>:1" in message
