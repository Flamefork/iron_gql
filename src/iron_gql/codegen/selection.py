from collections.abc import Callable
from collections.abc import Iterable
from typing import Any
from typing import cast

import graphql

from iron_gql.codegen.parser import GQLVar


def merge_selection_sets(
    field_nodes: list[graphql.FieldNode],
) -> graphql.SelectionSetNode | None:
    selections: list[graphql.SelectionNode] = []
    for node in field_nodes:
        if node.selection_set is not None:
            selections.extend(node.selection_set.selections)
    if not selections:
        return None
    return graphql.SelectionSetNode(selections=selections)


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
        if isinstance(typ, graphql.GraphQLInterfaceType):
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


class _DirectiveVarCollector(graphql.Visitor):
    def __init__(
        self,
        desired: dict[str, bool],
        targets: dict[str, bool],
        merge: Callable[[bool, bool], bool],
    ) -> None:
        super().__init__()
        self.desired = desired
        self.targets = targets
        self.merge = merge

    def enter_directive(self, node: graphql.DirectiveNode, *_args: object) -> None:
        target = self.targets.get(node.name.value)
        if target is None:
            return
        for arg in node.arguments or []:
            if arg.name.value != "if":
                continue
            if isinstance(arg.value, graphql.VariableNode):
                var_name = arg.value.name.value
                prev = self.desired.get(var_name)
                self.desired[var_name] = (
                    target if prev is None else self.merge(prev, target)
                )


def _collect_directive_variable_values(
    doc: graphql.DocumentNode,
    variables: Iterable[GQLVar],
    *,
    include_value: bool,
    skip_value: bool,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        var.name: var.default_value
        for var in variables
        if var.default_value != graphql.Undefined
    }
    desired: dict[str, bool] = {}
    collector = _DirectiveVarCollector(
        desired=desired,
        targets={"include": include_value, "skip": skip_value},
        merge=max if include_value else min,
    )
    graphql.visit(doc, collector)
    for name, value in desired.items():
        if name not in values:
            values[name] = value
    return values


def build_codegen_variable_values(
    doc: graphql.DocumentNode,
    variables: Iterable[GQLVar],
) -> dict[str, Any]:
    return _collect_directive_variable_values(
        doc, variables, include_value=True, skip_value=False
    )


def build_excluded_variable_values(
    doc: graphql.DocumentNode,
    variables: Iterable[GQLVar],
) -> dict[str, Any]:
    return _collect_directive_variable_values(
        doc, variables, include_value=False, skip_value=True
    )
