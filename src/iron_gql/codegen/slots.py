from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

import graphql

from iron_gql.codegen.accessors import type_info_type
from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import slot_roots
from iron_gql.codegen.selection import has_conditional_directive
from iron_gql.codegen.selection import response_key
from iron_gql.codegen.util import reachable

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
    # gets `None` at runtime instead of `()` when `graphql.visit` re-enters a
    # replacement node's own children -- as it does after `_SlotFiller`
    # (bindings.py) returns one.
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


class _SlotFieldFinder(graphql.Visitor):
    def __init__(self, found: list[graphql.FieldNode]) -> None:
        super().__init__()
        self.found = found

    def enter_field(self, node: graphql.FieldNode, *_args: object) -> None:
        if has_slot_directive(node):
            self.found.append(node)


def slot_fields(node: graphql.Node) -> list[graphql.FieldNode]:
    found: list[graphql.FieldNode] = []
    graphql.visit(node, _SlotFieldFinder(found))
    return found


@dataclass(kw_only=True, frozen=True)
class QuerySlot:
    name: str
    type_name: str


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


class _SlotCollector(graphql.Visitor):
    def __init__(
        self,
        *,
        by_name: dict[str, QuerySlot],
        type_info: graphql.TypeInfo,
        location: str,
    ) -> None:
        super().__init__()
        self.by_name = by_name
        self.type_info = type_info
        self.location = location

    def enter_field(self, node: graphql.FieldNode, *_args: object) -> None:
        if not has_slot_directive(node):
            return
        name = response_key(node)
        location = self.location
        # Composite first: a scalar field has no selection set to put
        # __typename in, so asking it for one would report a demand that
        # cannot be met instead of the reason it cannot.
        named = graphql.get_named_type(type_info_type(self.type_info))
        if not isinstance(named, graphql.GraphQLCompositeType):
            msg = f"Slot '{name}' at {location} is not on a composite field"
            raise GraphQLGenerationError([msg])
        if has_conditional_directive(node):
            msg = (
                f"Slot '{name}' at {location} cannot carry @skip/@include; "
                "a slot field is always requested — a caller that wants no "
                "fragment data passes an empty list"
            )
            raise GraphQLGenerationError([msg])
        if not _selects_typename(node):
            msg = f"Slot '{name}' at {location} must select __typename unconditionally"
            raise GraphQLGenerationError([msg])
        existing = self.by_name.get(name)
        if existing is not None and existing.type_name != named.name:
            msg = (
                f"Slot '{name}' at {location} covers fields of different types "
                f"({existing.type_name}, {named.name}); use an alias"
            )
            raise GraphQLGenerationError([msg])
        self.by_name[name] = QuerySlot(name=name, type_name=named.name)


def collect_query_slots(
    *,
    operation_def: graphql.OperationDefinitionNode,
    schema: graphql.GraphQLSchema,
    location: str,
) -> tuple[QuerySlot, ...]:
    by_name: dict[str, QuerySlot] = {}
    type_info = graphql.TypeInfo(schema)
    graphql.visit(
        operation_def,
        graphql.TypeInfoVisitor(
            type_info,
            _SlotCollector(by_name=by_name, type_info=type_info, location=location),
        ),
    )
    # Document order, which is the dict's insertion order here: the slot order
    # is public API — it fixes the type parameter list of `{Operation}Bound` —
    # so it follows what the developer wrote rather than an alphabetical
    # accident of the response keys. Places that need an order-independent
    # identity (the logical combination and the runtime binding key) sort for
    # themselves.
    return tuple(by_name.values())


def reachable_model_names(
    roots: Iterable[str], artifacts: dict[str, CollectedArtifact]
) -> set[str]:
    # The models reachable from the roots through NamedRef edges: a slot's or a
    # fragment's subtree, as consumed by the open-model set and the
    # nested-slot rule.
    return reachable(
        roots,
        # A dependency that is not an artifact is an enum: a leaf.
        lambda name: (dep for dep in artifacts[name].dependencies if dep in artifacts),
    )


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
    #
    # Walks `ir.templates`, not `ir.operations`: a slot can only ever appear
    # on a template — that is what `parser.classify_queries` means by the
    # word, and the two kinds have been separate types ever since — so an
    # operation has no slots to nest in the first place.
    artifacts = {artifact.name: artifact for artifact in ir.result_artifacts}
    slot_names = _slot_names_by_model(ir)
    errors: list[str] = []
    for template in ir.templates:
        at = template.location
        outer_names = (
            reachable_model_names([template.result_type], artifacts) & slot_names.keys()
        )
        # Collected as slot-name pairs rather than model pairs: a slot on a union
        # or interface has one model per variant, and all of them carry the same
        # `slot_name`, so reporting per model would repeat one nesting verbatim
        # once per variant.
        nested: set[tuple[str, str]] = set()
        for outer in outer_names:
            nested.update(
                (slot_names[outer], slot_names[inner])
                for inner in reachable_model_names([outer], artifacts)
                & slot_names.keys()
            )
        errors.extend(
            _nested_slot_error(
                template_name=template.class_name, at=at, outer=outer, inner=inner
            )
            for outer, inner in sorted(nested)
        )
    return errors


def _nested_slot_error(*, template_name: str, at: str, outer: str, inner: str) -> str:
    return (
        f"Slot '{inner}' of template '{template_name}' at {at} is nested inside "
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
