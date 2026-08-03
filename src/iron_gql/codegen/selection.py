import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import cast

import graphql

from iron_gql.codegen.slots import response_key

# A conjunction of @include/@skip literals: (variable name, required value)
# pairs. The empty conjunction always holds; `_conjoin` returns None for a
# contradiction, so every stored Cond is satisfiable.
type Cond = frozenset[tuple[str, bool]]

ALWAYS: Cond = frozenset()

# The selection sets a model is collected from, each with the condition under
# which its parent node is selected: a response key merged from several parent
# nodes contributes each parent's subtree under that parent's own condition.
type SelectionRoots = tuple[tuple[graphql.SelectionSetNode, Cond], ...]


@dataclass(frozen=True)
class ConditionalNode:
    node: graphql.FieldNode
    cond: Cond


def collect_conditional_fields(
    *,
    schema: graphql.GraphQLSchema,
    fragments: dict[str, graphql.FragmentDefinitionNode],
    runtime_type: graphql.GraphQLObjectType,
    roots: SelectionRoots,
) -> dict[str, list[ConditionalNode]]:
    # A symbolic mirror of graphql-core's collect_fields: nodes are grouped by
    # response key in document order, each with the exact condition under which
    # it is selected — conjoined from every @include/@skip on the way, plus the
    # root's own inherited condition. Sampled variable assignments cannot do
    # this job: one assignment per pass is a point in the assignment space,
    # and a node whose condition holds only between the sampled points is
    # invisible to every pass.
    walk = _FieldWalk(schema=schema, fragments=fragments, runtime_type=runtime_type)
    for selection_set, cond in roots:
        walk.selection_set(selection_set, cond)
    return walk.grouped


@dataclass(kw_only=True)
class _FieldWalk:
    schema: graphql.GraphQLSchema
    fragments: dict[str, graphql.FragmentDefinitionNode]
    runtime_type: graphql.GraphQLObjectType
    grouped: dict[str, list[ConditionalNode]] = field(default_factory=dict)
    _seen: set[tuple[int, Cond]] = field(default_factory=set)

    def selection_set(
        self, selection_set: graphql.SelectionSetNode, cond: Cond
    ) -> None:
        for selection in selection_set.selections:
            match selection:
                case graphql.FieldNode():
                    self._field(selection, cond)
                case graphql.InlineFragmentNode():
                    type_condition = cast(
                        "graphql.NamedTypeNode | None", selection.type_condition
                    )
                    self._nested(
                        selection, selection.selection_set, type_condition, cond
                    )
                case graphql.FragmentSpreadNode():
                    fragment = self.fragments[selection.name.value]
                    self._nested(
                        selection, fragment.selection_set, fragment.type_condition, cond
                    )
                case _:
                    msg = f"Unsupported selection node: {selection}"
                    raise TypeError(msg)

    def _field(self, selection: graphql.FieldNode, cond: Cond) -> None:
        node_cond = _selection_cond(selection, cond)
        if node_cond is None or (id(selection), node_cond) in self._seen:
            return
        self._seen.add((id(selection), node_cond))
        self.grouped.setdefault(response_key(selection), []).append(
            ConditionalNode(node=selection, cond=node_cond)
        )

    def _nested(
        self,
        selection: graphql.InlineFragmentNode | graphql.FragmentSpreadNode,
        selection_set: graphql.SelectionSetNode,
        type_condition: graphql.NamedTypeNode | None,
        cond: Cond,
    ) -> None:
        if not _type_condition_matches(self.schema, type_condition, self.runtime_type):
            return
        node_cond = _selection_cond(selection, cond)
        if node_cond is not None:
            self.selection_set(selection_set, node_cond)


def uncovered_assignment(
    base: Sequence[Cond], cover: Sequence[Cond]
) -> dict[str, bool] | None:
    # A witness assignment under which some `base` conjunction holds but no
    # `cover` conjunction does; None when `cover` covers `base` everywhere.
    # Enumeration is exact and exponential in the number of distinct
    # variables conditioning a single response key — a handful in any real
    # query; a document conditioning one key on dozens of variables would
    # make this generation-time walk crawl, and that is an accepted bound.
    names = sorted({name for cond in (*base, *cover) for name, _ in cond})
    for values in itertools.product((False, True), repeat=len(names)):
        assignment = dict(zip(names, values, strict=True))
        if _holds(base, assignment) and not _holds(cover, assignment):
            return assignment
    return None


def _holds(conds: Sequence[Cond], assignment: dict[str, bool]) -> bool:
    return any(
        all(assignment[name] is required for name, required in cond) for cond in conds
    )


def _selection_cond(
    selection: graphql.FieldNode
    | graphql.InlineFragmentNode
    | graphql.FragmentSpreadNode,
    base: Cond,
) -> Cond | None:
    literals = _directive_literals(selection)
    if literals is None:
        return None
    return _conjoin(base, literals)


def _directive_literals(
    selection: graphql.FieldNode
    | graphql.InlineFragmentNode
    | graphql.FragmentSpreadNode,
) -> list[tuple[str, bool]] | None:
    # None: a literal boolean if-argument excludes the node statically. A
    # variable argument becomes a literal of the conjunction; validation has
    # already pinned the argument to a boolean variable or a boolean value.
    literals: list[tuple[str, bool]] = []
    for directive in selection.directives or ():
        if directive.name.value not in {"include", "skip"}:
            continue
        required = directive.name.value == "include"
        for argument in directive.arguments or ():
            if argument.name.value != "if":
                continue
            value = argument.value
            if isinstance(value, graphql.BooleanValueNode):
                if value.value is not required:
                    return None
            elif isinstance(value, graphql.VariableNode):
                literals.append((value.name.value, required))
    return literals


def _conjoin(cond: Cond, literals: list[tuple[str, bool]]) -> Cond | None:
    merged = dict(cond)
    for name, required in literals:
        if merged.setdefault(name, required) is not required:
            return None
    return frozenset(merged.items())


def _type_condition_matches(
    schema: graphql.GraphQLSchema,
    type_condition: graphql.NamedTypeNode | None,
    runtime_type: graphql.GraphQLObjectType,
) -> bool:
    if type_condition is None:
        return True
    named = schema.get_type(type_condition.name.value)
    if named is runtime_type:
        return True
    match named:
        case graphql.GraphQLInterfaceType() | graphql.GraphQLUnionType():
            return schema.is_sub_type(named, runtime_type)
        case _:
            return False


def resolve_explicit_types(
    *,
    schema: graphql.GraphQLSchema,
    selection_set: graphql.SelectionSetNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
    interface_type: graphql.GraphQLInterfaceType,
    possible_types: list[graphql.GraphQLObjectType],
) -> set[graphql.GraphQLObjectType]:
    explicit_conditions = collect_type_conditions(selection_set, fragments) - {
        interface_type.name
    }
    explicit_objects: set[graphql.GraphQLObjectType] = set()
    for name in explicit_conditions:
        typ = schema.get_type(name)
        if typ is None:
            msg = f"Unknown GraphQL type '{name}'"
            raise ValueError(msg)
        if isinstance(typ, graphql.GraphQLObjectType):
            explicit_objects.add(typ)
            continue
        if isinstance(typ, graphql.GraphQLInterfaceType | graphql.GraphQLUnionType):
            possible_set = set(possible_types)
            explicit_objects.update(set(schema.get_possible_types(typ)) & possible_set)
            continue
        msg = f"Type condition '{name}' is not a composite type"
        raise TypeError(msg)
    return explicit_objects


def collect_type_conditions(
    selection_set: graphql.SelectionSetNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> set[str]:
    conditions: set[str] = set()
    visited_fragments: set[str] = set()
    stack: list[graphql.SelectionSetNode] = [selection_set]
    while stack:
        for selection in stack.pop().selections:
            match selection:
                case graphql.FieldNode():
                    pass
                case graphql.InlineFragmentNode():
                    type_condition = cast(
                        graphql.NamedTypeNode | None, selection.type_condition
                    )
                    if type_condition is not None:
                        conditions.add(type_condition.name.value)
                    stack.append(selection.selection_set)
                case graphql.FragmentSpreadNode():
                    name = selection.name.value
                    if name in visited_fragments:
                        continue
                    visited_fragments.add(name)
                    fragment = fragments.get(name)
                    if fragment is None:
                        continue
                    conditions.add(fragment.type_condition.name.value)
                    stack.append(fragment.selection_set)
                case _:
                    msg = f"Unsupported selection node: {selection}"
                    raise TypeError(msg)
    return conditions


def interface_has_base_typename(
    selection_set: graphql.SelectionSetNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
    interface_name: str,
) -> bool:
    visited_fragments: set[str] = set()
    stack: list[graphql.SelectionSetNode] = [selection_set]
    while stack:
        for selection in stack.pop().selections:
            match selection:
                case graphql.FieldNode():
                    if selection.alias is None and selection.name.value == "__typename":
                        return True
                case graphql.InlineFragmentNode():
                    type_cond = cast(
                        graphql.NamedTypeNode | None, selection.type_condition
                    )
                    if type_cond is None or type_cond.name.value == interface_name:
                        stack.append(selection.selection_set)
                case graphql.FragmentSpreadNode():
                    name = selection.name.value
                    if name in visited_fragments:
                        continue
                    visited_fragments.add(name)
                    fragment = fragments.get(name)
                    if (
                        fragment is not None
                        and fragment.type_condition.name.value == interface_name
                    ):
                        stack.append(fragment.selection_set)
                case _:
                    msg = f"Unsupported selection node: {selection}"
                    raise TypeError(msg)
    return False
