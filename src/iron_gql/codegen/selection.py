import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field

import graphql

from iron_gql.codegen.accessors import inline_fragment_type_condition

# The two schema directives that make a selection conditional. Named once: the
# condition algebra below reads them, a slot field may not carry them at all
# (a slot is always requested, see `slots._SlotCollector.enter_field`), and a
# fragment spread a bind reaches at a slot's root may not carry them at the
# typenames that spread is the only way there -- where the same fragment also
# reaches the root unconditionally, its fields are always requested at those
# typenames and the conditional path adds nothing to be wrong about (see
# `bindings._readable_fragments`).
CONDITIONAL_DIRECTIVE_NAMES = frozenset({"include", "skip"})


def has_conditional_directive(
    node: graphql.FieldNode | graphql.InlineFragmentNode | graphql.FragmentSpreadNode,
) -> bool:
    return any(
        directive.name.value in CONDITIONAL_DIRECTIVE_NAMES
        for directive in node.directives or ()
    )


def response_key(node: graphql.FieldNode) -> str:
    return node.alias.value if node.alias else node.name.value


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
                    type_condition = inline_fragment_type_condition(selection)
                    self._nested(
                        selection, selection.selection_set, type_condition, cond
                    )
                case graphql.FragmentSpreadNode():
                    fragment = self.fragments[selection.name.value]
                    self._nested(
                        selection, fragment.selection_set, fragment.type_condition, cond
                    )
                case _:
                    # Internal invariant, not a diagnosis: a selection is one
                    # of exactly three node kinds, and every document walked
                    # here came out of graphql-core's own parser. The case
                    # stays because the kinds are subclasses of one node type,
                    # which no type checker can see as exhausted.
                    msg = f"Unsupported selection node: {selection}"
                    raise AssertionError(msg)

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
    # variable argument becomes a literal of the conjunction.
    literals: list[tuple[str, bool]] = []
    for directive in selection.directives or ():
        if directive.name.value not in CONDITIONAL_DIRECTIVE_NAMES:
            continue
        required = directive.name.value == "include"
        # Both directives declare exactly one argument, `if: Boolean!`, and
        # validation has already rejected a document that names another one or
        # gives it anything but a boolean literal or a boolean variable -- so
        # the loop needs no name test, and a third shape of value is an
        # internal invariant to crash on, not a case with a meaning.
        for argument in directive.arguments or ():
            match argument.value:
                case graphql.BooleanValueNode() as literal:
                    if literal.value is not required:
                        return None
                case graphql.VariableNode() as variable:
                    literals.append((variable.name.value, required))
                case unexpected:
                    msg = f"@{directive.name.value}(if:) is {unexpected.kind}"
                    raise AssertionError(msg)
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
    possible_set = set(possible_types)
    explicit_objects: set[graphql.GraphQLObjectType] = set()
    for name in explicit_conditions:
        named = schema.get_type(name)
        match named:
            case graphql.GraphQLObjectType():
                explicit_objects.add(named)
            case graphql.GraphQLInterfaceType() | graphql.GraphQLUnionType():
                explicit_objects.update(
                    set(schema.get_possible_types(named)) & possible_set
                )
            case _:
                # Internal invariant, not a diagnosis: every name here is a
                # type condition of a validated document, so the schema
                # resolves it and validation has already rejected a condition
                # on anything but a composite type.
                msg = f"Type condition '{name}' is not a composite type"
                raise AssertionError(msg)
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
                    type_condition = inline_fragment_type_condition(selection)
                    if type_condition is not None:
                        conditions.add(type_condition.name.value)
                    stack.append(selection.selection_set)
                case graphql.FragmentSpreadNode():
                    name = selection.name.value
                    # Once per walk: a diamond spreads the same fragment down
                    # two paths, and the second visit can only re-add the
                    # conditions the first already collected.
                    if name in visited_fragments:
                        continue
                    visited_fragments.add(name)
                    # Indexed, not `.get`: a spread whose definition is
                    # missing never survives validation -- the assumption
                    # `_FieldWalk.selection_set` walks on over the same map.
                    fragment = fragments[name]
                    conditions.add(fragment.type_condition.name.value)
                    stack.append(fragment.selection_set)
                case _:
                    # The three selection kinds again, see
                    # `_FieldWalk.selection_set`.
                    msg = f"Unsupported selection node: {selection}"
                    raise AssertionError(msg)
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
                    type_cond = inline_fragment_type_condition(selection)
                    if type_cond is None or type_cond.name.value == interface_name:
                        stack.append(selection.selection_set)
                case graphql.FragmentSpreadNode():
                    name = selection.name.value
                    # Once per walk, like `collect_type_conditions`: the
                    # second visit re-walks a subtree that has already had its
                    # say about the base __typename.
                    if name in visited_fragments:
                        continue
                    visited_fragments.add(name)
                    # Only a fragment conditioned on the interface itself is
                    # part of the base selection; one on a concrete type
                    # carries its __typename into that variant alone, which is
                    # not what the caller is asking about.
                    fragment = fragments[name]
                    if fragment.type_condition.name.value == interface_name:
                        stack.append(fragment.selection_set)
                case _:
                    # The three selection kinds again, see
                    # `_FieldWalk.selection_set`.
                    msg = f"Unsupported selection node: {selection}"
                    raise AssertionError(msg)
    return False
