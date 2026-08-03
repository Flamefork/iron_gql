from dataclasses import dataclass
from typing import cast

import graphql

from iron_gql.codegen.accessors import type_info_type
from iron_gql.codegen.accessors import visit_document
from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import slot_roots

SLOT_DIRECTIVE_NAME = "slot"
SLOT_DIRECTIVE_SDL = "directive @slot on FIELD"


def extend_schema_with_slot(schema: graphql.GraphQLSchema) -> graphql.GraphQLSchema:
    # Slots are a codegen-level concept: the directive never reaches the server,
    # so it is declared only for the validation pass. A schema may already
    # declare it — accepted when the declaration means the same thing, rejected
    # with a diagnosis otherwise, instead of surfacing graphql-core's raw
    # redefinition TypeError from a package that may not even use slots.
    existing = schema.get_directive(SLOT_DIRECTIVE_NAME)
    if existing is None:
        return graphql.extend_schema(schema, graphql.parse(SLOT_DIRECTIVE_SDL))
    if existing.locations == (graphql.DirectiveLocation.FIELD,) and not existing.args:
        return schema
    msg = (
        f"The schema declares @{SLOT_DIRECTIVE_NAME} differently from "
        f"iron_gql's '{SLOT_DIRECTIVE_SDL}'; align the declaration or drop it"
    )
    raise GraphQLGenerationError([msg])


def has_slot_directive(node: graphql.FieldNode) -> bool:
    # graphql-core types `directives` as a plain tuple, but a `FieldNode`
    # built without passing the argument (as the marker field below does, and
    # as `graphql.visit` does when re-entering a replacement node's own
    # children) gets `None` at runtime instead of `()`.
    return any(
        directive.name.value == SLOT_DIRECTIVE_NAME
        for directive in node.directives or ()
    )


def without_slot_directive(
    node: graphql.FieldNode,
) -> tuple[graphql.DirectiveNode, ...]:
    return tuple(
        directive
        for directive in node.directives or ()
        if directive.name.value != SLOT_DIRECTIVE_NAME
    )


def response_key(node: graphql.FieldNode) -> str:
    return node.alias.value if node.alias else node.name.value


def slot_fields(node: graphql.Node) -> list[graphql.FieldNode]:
    found: list[graphql.FieldNode] = []

    class Finder(graphql.Visitor):
        def enter_field(self, node: graphql.FieldNode, *_args: object) -> None:
            if has_slot_directive(node):
                found.append(node)

    graphql.visit(node, Finder())
    return found


@dataclass(kw_only=True, frozen=True)
class QuerySlot:
    name: str
    type_name: str


def build_exec_document(
    doc: graphql.DocumentNode,
) -> tuple[graphql.DocumentNode, tuple[tuple[str, str], ...]]:
    # Each @slot occurrence gets its own indexed token rather than a
    # name-derived one: the token only exists to be located in the printed
    # text once, and `build_exec_parts` verifies that uniqueness.
    markers: list[tuple[str, str]] = []

    class SlotStripper(graphql.Visitor):
        def enter_field(self, node: graphql.FieldNode, *_args: object) -> object:
            if not has_slot_directive(node):
                return None
            token = f"__slot__{len(markers)}__"
            markers.append((token, response_key(node)))
            selections: list[graphql.SelectionNode] = [
                *(node.selection_set.selections if node.selection_set else []),
                graphql.FieldNode(name=graphql.NameNode(value=token)),
            ]
            return graphql.FieldNode(
                alias=node.alias,
                name=node.name,
                arguments=node.arguments,
                directives=without_slot_directive(node),
                selection_set=graphql.SelectionSetNode(selections=selections),
            )

    return visit_document(doc, SlotStripper()), tuple(markers)


def build_exec_parts(
    doc: graphql.DocumentNode,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    # The printed operation is split at the synthesized marker fields here, at
    # codegen time, so the runtime splices spreads purely positionally and
    # never matches text — user-written text that merely looks like a marker
    # stays untouched. Each token is verified to occur exactly once, so a
    # literal collision is a loud error instead of a silent mis-split.
    # Returned as the head plus (slot name, following text) pairs, so a gap
    # and its slot name cannot fall out of sync.
    exec_doc, markers = build_exec_document(doc)
    printed = graphql.print_ast(exec_doc)
    positioned: list[tuple[int, str, str]] = []
    for token, slot_name in markers:
        if printed.count(token) != 1:
            msg = (
                f"Operation text contains the reserved marker token '{token}';"
                " remove it so slot spreads can be spliced unambiguously"
            )
            raise GraphQLGenerationError([msg])
        positioned.append((printed.index(token), token, slot_name))
    # Textual order, not visitor order: for (invalid, rejected later) nested
    # slots the outer marker prints after the inner subtree, and the split
    # must not depend on that.
    positioned.sort()
    segments: list[str] = []
    slot_names: list[str] = []
    rest = printed
    for _position, token, slot_name in positioned:
        before, rest = rest.split(token, maxsplit=1)
        segments.append(before)
        slot_names.append(slot_name)
    segments.append(rest)
    return segments[0], tuple(zip(slot_names, segments[1:], strict=True))


def possible_type_names(
    schema: graphql.GraphQLSchema, type_name: str
) -> frozenset[str]:
    named = schema.get_type(type_name)
    match named:
        case graphql.GraphQLObjectType():
            return frozenset({named.name})
        case graphql.GraphQLUnionType() | graphql.GraphQLInterfaceType():
            return frozenset(
                possible.name for possible in schema.get_possible_types(named)
            )
        case _:
            # Internal invariant, not a diagnosis: every caller passes a type
            # name that validation has already resolved to a composite type.
            msg = f"'{type_name}' is not a composite type"
            raise AssertionError(msg)


def fragment_base_name(type_name: str) -> str:
    return f"{type_name}Fragment"


def spreads_into(
    schema: graphql.GraphQLSchema, fragment_type: str, target_type: str
) -> bool:
    # GraphQL's own spread rule, stated once: a fragment on X may be spread
    # into a selection on C exactly when their possible types overlap.
    # Uniform for objects, interfaces and unions — no case analysis needed.
    return bool(
        possible_type_names(schema, fragment_type)
        & possible_type_names(schema, target_type)
    )


def collect_query_slots(
    *,
    operation_def: graphql.OperationDefinitionNode,
    schema: graphql.GraphQLSchema,
    location: str,
) -> tuple[QuerySlot, ...]:
    by_name: dict[str, QuerySlot] = {}
    type_info = graphql.TypeInfo(schema)

    class SlotCollector(graphql.Visitor):
        def enter_field(self, node: graphql.FieldNode, *_args: object) -> None:
            if not has_slot_directive(node):
                return
            name = response_key(node)
            # Composite first: a scalar field has no selection set to put
            # __typename in, so asking it for one would report a demand that
            # cannot be met instead of the reason it cannot.
            named = graphql.get_named_type(type_info_type(type_info))
            if not isinstance(named, graphql.GraphQLCompositeType):
                msg = f"Slot '{name}' at {location} is not on a composite field"
                raise GraphQLGenerationError([msg])
            if any(
                directive.name.value in {"include", "skip"}
                for directive in node.directives or ()
            ):
                msg = (
                    f"Slot '{name}' at {location} cannot carry @skip/@include; "
                    "a slot field is always requested — a caller that wants no "
                    "fragment data passes an empty list"
                )
                raise GraphQLGenerationError([msg])
            if not _selects_typename(node):
                msg = (
                    f"Slot '{name}' at {location} must select __typename "
                    "unconditionally"
                )
                raise GraphQLGenerationError([msg])
            existing = by_name.get(name)
            if existing is not None and existing.type_name != named.name:
                msg = (
                    f"Slot '{name}' at {location} covers fields of different types "
                    f"({existing.type_name}, {named.name}); use an alias"
                )
                raise GraphQLGenerationError([msg])
            by_name[name] = QuerySlot(name=name, type_name=named.name)

    graphql.visit(operation_def, graphql.TypeInfoVisitor(type_info, SlotCollector()))
    return tuple(by_name[name] for name in sorted(by_name))


def reachable_model_names(
    root: str, artifacts: dict[str, CollectedArtifact]
) -> set[str]:
    # The models reachable from a root through NamedRef edges: a slot's or a
    # fragment's subtree, as consumed by the open-model set and the
    # nested-slot rule.
    reached: set[str] = set()
    queue = [root]
    while queue:
        name = queue.pop()
        for dependency in artifacts[name].dependencies:
            # A dependency that is not an artifact is an enum: a leaf.
            if dependency in reached or dependency not in artifacts:
                continue
            reached.add(dependency)
            queue.append(dependency)
    return reached


def _slot_names_by_model(ir: CollectedPackageIR) -> dict[str, str]:
    return {
        model.name: slot_name for model, slot_name in slot_roots(ir.result_artifacts)
    }


def validate_no_nested_slots(ir: CollectedPackageIR) -> list[str]:
    # Expressed over the collected models rather than the AST: collect has
    # already merged the field nodes of a response key, so a slot reached
    # through a merged key — `x @slot` on one node and `x { y @slot }` on
    # another — is a plain edge here instead of an AST shape to reconstruct.
    # A nested slot entangles two composition points: the inner field is part
    # of the outer slot's static selection, so every fragment passed to the
    # outer overlaps the inner's payload and splice point — rejected instead
    # of being given accidental semantics.
    artifacts = {artifact.name: artifact for artifact in ir.result_artifacts}
    slot_names = _slot_names_by_model(ir)
    errors: list[str] = []
    for operation in ir.operations:
        at = ", ".join(operation.locations)
        outer_names = reachable_model_names(operation.result_type, artifacts) & set(
            slot_names
        )
        # Collected as slot-name pairs rather than model pairs: a slot on a union
        # or interface has one model per variant, and all of them carry the same
        # `slot_name`, so reporting per model would repeat one nesting verbatim
        # once per variant.
        nested: set[tuple[str, str]] = set()
        for outer in outer_names:
            nested.update(
                (slot_names[outer], slot_names[inner])
                for inner in reachable_model_names(outer, artifacts) & set(slot_names)
            )
        errors.extend(
            _nested_slot_error(
                operation_name=operation.class_name, at=at, outer=outer, inner=inner
            )
            for outer, inner in sorted(nested)
        )
    return errors


def validate_slots_are_collected(ir: CollectedPackageIR) -> list[str]:
    # A slot kwarg and its `{Type}Fragment` base come from the AST, but the
    # slot model only exists when the field survives collection — a literal
    # @skip/@include drops it, leaving an operation that demands a handle
    # whose data can never arrive and a rendered module that references the
    # never-imported slot runtime.
    artifacts = {artifact.name: artifact for artifact in ir.result_artifacts}
    slot_names = _slot_names_by_model(ir)
    errors: list[str] = []
    for operation in ir.operations:
        at = ", ".join(operation.locations)
        collected = {
            slot_names[name]
            for name in reachable_model_names(operation.result_type, artifacts)
            if name in slot_names
        }
        errors.extend(
            _excluded_slot_error(
                slot_name=slot.name, operation_name=operation.class_name, at=at
            )
            for slot in operation.slots
            if slot.name not in collected
        )
    return errors


def _excluded_slot_error(*, slot_name: str, operation_name: str, at: str) -> str:
    return (
        f"Slot '{slot_name}' of operation '{operation_name}' at {at} is "
        "statically excluded by its @skip/@include directives"
    )


def _nested_slot_error(*, operation_name: str, at: str, outer: str, inner: str) -> str:
    return (
        f"Slot '{inner}' of operation '{operation_name}' at {at} is nested inside "
        f"slot '{outer}'; a slot cannot sit inside another slot's subtree"
    )


def _selects_typename(node: graphql.FieldNode) -> bool:
    # A composite field always has a selection set here — ScalarLeafs rejected
    # the document before slot collection otherwise — so the AST-level
    # Optional is vacuous; a violated premise crashes instead of turning into
    # a "must select __typename" misdiagnosis.
    selection_set = cast("graphql.SelectionSetNode", node.selection_set)
    return any(
        isinstance(selection, graphql.FieldNode)
        and selection.alias is None
        and selection.name.value == "__typename"
        and not selection.directives
        for selection in selection_set.selections
    )
