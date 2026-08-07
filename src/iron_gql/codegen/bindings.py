from collections import defaultdict
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass

import graphql

from iron_gql.codegen.accessors import inline_fragment_type_condition
from iron_gql.codegen.accessors import type_info_input_type
from iron_gql.codegen.accessors import visit_document
from iron_gql.codegen.accessors import wrapping_of_type
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.parser import collect_fragments_from_doc
from iron_gql.codegen.parser import collect_transitive_fragment_names
from iron_gql.codegen.selection import has_conditional_directive
from iron_gql.codegen.selection import response_key
from iron_gql.codegen.slots import has_slot_directive
from iron_gql.codegen.slots import possible_type_names
from iron_gql.codegen.slots import spreads_into
from iron_gql.codegen.slots import without_slot_directive


@dataclass(kw_only=True, frozen=True)
class SlotTarget:
    # One template slot as a bind sees it. The mapping that holds these is
    # keyed by the `bind()` keyword (the slot's `python_name`), because that is
    # the name a developer wrote and the only one a diagnosis may quote back;
    # `response_key` is the wire-side name the spread is actually written at.
    type_name: str
    response_key: str


@dataclass(kw_only=True, frozen=True)
class ReadableFragment:
    # One fragment offered to one slot, with the runtime typenames at which its
    # fields are actually present on that slot's root payload. That is not the
    # fragment's own type condition: reaching the slot root through a narrower
    # condition -- an interface brick spread inside a per-type fragment -- cuts
    # it down, and every typename outside the cut is a payload where the
    # fragment's fields are legitimately absent.
    name: str
    typenames: frozenset[str]
    # Whether the bind named this fragment for this slot, as opposed to
    # reaching it through another bound fragment's root-level spread. The one
    # place the distinction is recorded: `bind()`'s overloads, the binding's
    # class name and the runtime dispatch key are built from the direct subset,
    # while the whole set is offered to validation. Kept as a flag on the
    # readable entry rather than a second list beside it -- the two would be
    # kept in step only by an invariant no layer states.
    direct: bool


@dataclass(kw_only=True, frozen=True)
class SynthesizedVar:
    # One fragment variable the expansion declares on the operation, paired
    # with the one fact that separates it from a template variable. Paired
    # here, and not handed on as a list of nodes beside a set of names, so no
    # layer downstream has to re-join the two by raw GraphQL name -- a join
    # that keeps type-checking and silently stops matching the day a variable
    # is identified by anything but that name.
    node: graphql.VariableDefinitionNode
    # Whether every position this variable fills declares a schema default.
    # Those are the only fragment variables a caller may leave out of
    # `with_args`, and the only ones whose absence must reach the server as an
    # absent variable rather than an explicit null -- absent is what lets the
    # schema's default apply, and a non-null position rejects null outright.
    # Every other fragment variable behaves exactly like an operation variable
    # of `execute`: always sent, `None` meaning `null`.
    omittable: bool


@dataclass(kw_only=True, frozen=True)
class ExpandedBinding:
    exec_source: str
    # The fragments readable at each slot's root, keyed by the slot's response
    # key and sorted by fragment name. Wider than what the bind named -- a
    # fragment reached only through another fragment's root-level spread is
    # readable through its own handle too -- but narrower than the document
    # closure: a fragment spread inside a *field* of a bound fragment lands
    # under that field in the payload, not on the slot root, so no handle can
    # read it there. Which of them the bind named literally is
    # `ReadableFragment.direct`.
    #
    # Total over the *template's* slots, not over the bind's arguments: a slot
    # this bind left empty has an entry of its own, an empty one. That is what
    # lets `collect._binding_slot`, which walks every template slot, index this
    # map -- read with a default instead, "the bind filled no fragments here"
    # and "the two sides key by different names" would be one answer.
    readable_fragments: dict[str, tuple[ReadableFragment, ...]]
    fragment_vars: tuple[SynthesizedVar, ...]


class _SlotFiller(graphql.Visitor):
    # `names_by_key` is total over the template's slots, so an unfilled slot is
    # an empty tuple here and the lookup below is a plain index: every `@slot`
    # node of this document was collected into the template's slots by
    # `collect_query_slots` (and `validate_no_slots_in_fragments` keeps the
    # directive out of the definitions that walk does not visit), so a key that
    # misses is the two namespaces having drifted apart -- which must crash,
    # not quietly print a document with the spreads left out.
    def __init__(self, names_by_key: Mapping[str, tuple[str, ...]]) -> None:
        super().__init__()
        self.names_by_key = names_by_key

    def enter_field(self, node: graphql.FieldNode, *_args: object) -> object:
        if not has_slot_directive(node):
            return None
        names = self.names_by_key[response_key(node)]
        selections: list[graphql.SelectionNode] = [
            *(node.selection_set.selections if node.selection_set else []),
            *(
                graphql.FragmentSpreadNode(name=graphql.NameNode(value=name))
                for name in names
            ),
        ]
        return graphql.FieldNode(
            alias=node.alias,
            name=node.name,
            arguments=node.arguments,
            directives=without_slot_directive(node),
            selection_set=graphql.SelectionSetNode(selections=selections),
        )


def _slot_arg_errors(
    *,
    schema: graphql.GraphQLSchema,
    key: str,
    fragments: tuple[graphql.FragmentDefinitionNode, ...],
    slots: Mapping[str, SlotTarget],
    location: str,
) -> list[str]:
    if key not in slots:
        names = ", ".join(sorted(slots))
        # An unknown key has no slot type, so its fragments cannot be checked
        # against one -- every further diagnosis for this key would be about a
        # slot that does not exist.
        return [
            (
                f"Bind at {location} targets unknown slot '{key}'; the "
                f"template's slots are: {names}"
            )
        ]
    slot_type = slots[key].type_name
    return [
        (
            f"Bind at {location}: fragment '{fragment.name.value}' on "
            f"'{fragment.type_condition.name.value}' cannot be spread into "
            f"slot '{key}' of type '{slot_type}'"
        )
        for fragment in fragments
        if not spreads_into(schema, fragment.type_condition.name.value, slot_type)
    ]


def _validate_slot_args(
    *,
    schema: graphql.GraphQLSchema,
    slots: Mapping[str, SlotTarget],
    spreads: Mapping[str, tuple[graphql.FragmentDefinitionNode, ...]],
    location: str,
) -> None:
    # Accumulated across every slot of the bind: one broken slot must not hide
    # the others, and a fragment incompatible with one slot says nothing about
    # the next fragment.
    errors = [
        error
        for key, fragments in sorted(spreads.items())
        for error in _slot_arg_errors(
            schema=schema,
            key=key,
            fragments=fragments,
            slots=slots,
            location=location,
        )
    ]
    if errors:
        raise GraphQLGenerationError(errors)


def _closure_fragments(
    *,
    direct_names: Iterable[str],
    fragments: Mapping[str, graphql.FragmentDefinitionNode],
    location: str,
) -> tuple[graphql.FragmentDefinitionNode, ...]:
    # The *document* closure: every definition the expanded operation has to
    # carry, position-blind by necessity -- a fragment spread inside a field of
    # a bound fragment is still a definition the document needs. Which of these
    # are readable through their own handle at a slot is a different question,
    # answered by `_readable_fragments` over the same namespace.
    closure_names, missing = collect_transitive_fragment_names(direct_names, fragments)
    if missing:
        # Internal invariant: `parser.bind_closures` walked this very closure
        # over this very namespace and reported every unresolvable name, so a
        # bind that reaches this far has none left.
        msg = f"unresolvable fragment(s) in the closure of a bind at {location}"
        raise AssertionError(msg)
    return tuple(fragments[name] for name in sorted(closure_names))


@dataclass(kw_only=True, frozen=True)
class _RootLevelSpread:
    name: str
    # The typenames still possible at the spread, after every type condition
    # on the way down from the enclosing fragment's own root.
    typenames: frozenset[str]
    conditional: bool


def _root_level_spreads(
    definition: graphql.FragmentDefinitionNode,
    *,
    schema: graphql.GraphQLSchema,
    typenames: frozenset[str],
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> list[_RootLevelSpread]:
    # The fragments this one spreads *at its own root level*: through inline
    # fragments, which only narrow the type, but never down through a field,
    # which moves the data one level deeper in the payload. Each carries the
    # narrowing accumulated on the way and whether any step of that way was
    # conditional.
    found: list[_RootLevelSpread] = []

    def walk(
        selection_set: graphql.SelectionSetNode,
        current: frozenset[str],
        *,
        conditional: bool,
    ) -> None:
        for selection in selection_set.selections:
            match selection:
                case graphql.FieldNode():
                    continue
                case graphql.InlineFragmentNode():
                    type_condition = inline_fragment_type_condition(selection)
                    walk(
                        selection.selection_set,
                        current
                        if type_condition is None
                        else current
                        & possible_type_names(schema, type_condition.name.value),
                        conditional=conditional or has_conditional_directive(selection),
                    )
                case graphql.FragmentSpreadNode():
                    name = selection.name.value
                    spread_on = fragments[name].type_condition.name.value
                    found.append(
                        _RootLevelSpread(
                            name=name,
                            typenames=current & possible_type_names(schema, spread_on),
                            conditional=conditional
                            or has_conditional_directive(selection),
                        )
                    )
                case _:
                    # Internal invariant: GraphQL has exactly three selection
                    # kinds, and the document parsed.
                    msg = f"Unsupported selection node: {selection}"
                    raise AssertionError(msg)

    walk(definition.selection_set, typenames, conditional=False)
    return found


def _readable_fragments(
    *,
    schema: graphql.GraphQLSchema,
    slot_type: str,
    direct: tuple[graphql.FragmentDefinitionNode, ...],
    fragments: dict[str, graphql.FragmentDefinitionNode],
    location: str,
) -> tuple[tuple[ReadableFragment, ...], list[str]]:
    # Walks out from the slot's directly-bound fragments through root-level
    # spreads only, unioning the typenames each fragment is reachable at.
    # Fragment cycles are rejected by GraphQL validation long before this, so
    # the walk terminates without a visited set.
    #
    # The walk accumulates and decides nothing: whether a fragment is readable
    # at this slot's root is a fact about *every* path to it, so a single edge
    # is not enough to answer it. Conditional edges are collected and judged
    # below, once the unconditional reachability is final.
    typenames_by_name: dict[str, set[str]] = defaultdict(set)
    conditional_edges: dict[tuple[str, str], set[str]] = defaultdict(set)

    def visit(name: str, typenames: frozenset[str]) -> None:
        if not typenames:
            # Reachable on paper, at no typename this slot can actually hold:
            # the fragment's fields are never present on this slot's payload,
            # so it is not readable here and gets no handle. A brick reused
            # across slots reaches this legitimately -- it is a fact about the
            # types, not a defect in the bind -- and registering it anyway
            # produced a handle whose `read` returned None for every response.
            return
        known = typenames_by_name[name]
        if typenames <= known:
            return
        known |= typenames
        for spread in _root_level_spreads(
            fragments[name], schema=schema, typenames=typenames, fragments=fragments
        ):
            if spread.conditional:
                # The typenames travel with the edge: what the unconditional
                # walk has to cover is not the fragment's name but every
                # typename this edge would deliver it at.
                conditional_edges[name, spread.name] |= spread.typenames
                continue
            visit(spread.name, spread.typenames)

    slot_typenames = possible_type_names(schema, slot_type)
    direct_names = {fragment.name.value for fragment in direct}
    for fragment in direct:
        visit(
            fragment.name.value,
            slot_typenames
            & possible_type_names(schema, fragment.type_condition.name.value),
        )
    # A conditional edge is only a problem at the typenames it *alone* would
    # deliver its fragment at. Where the unconditional walk already reaches it,
    # the fields are always requested and the handle is always safe to
    # validate, so the conditional path adds nothing there. Everywhere else the
    # handle's typenames stop short of the payloads the condition produces:
    # asking by name alone, a brick reached unconditionally on one type and
    # conditionally on another passed the check, and `read` then answered None
    # on a payload that carried the fields.
    errors = [
        _conditional_spread_error(
            outer=outer,
            spread=spread,
            uncovered=uncovered,
            location=location,
        )
        for (outer, spread), typenames in conditional_edges.items()
        if (uncovered := typenames - typenames_by_name.get(spread, set()))
    ]
    return (
        tuple(
            ReadableFragment(
                name=name,
                typenames=frozenset(typenames),
                direct=name in direct_names,
            )
            for name, typenames in sorted(typenames_by_name.items())
        ),
        errors,
    )


def _conditional_spread_error(
    *, outer: str, spread: str, uncovered: set[str], location: str
) -> str:
    # A fragment readable at a slot's root is validated at the response
    # boundary whenever the payload's `__typename` matches it -- that is what
    # makes it independently readable through its own handle. A conditional
    # spread breaks the premise: the server leaves those fields out when the
    # condition is false, so the handle's required fields are missing and
    # validation fails for a response that is entirely correct. Rejected for
    # the same reason a slot field may not carry @skip/@include (see
    # `collect_query_slots`): a bound fragment is always requested, and "no
    # data for this fragment" is spelled by not binding it.
    #
    # Only root-level paths are rejected. The same directive on a spread
    # nested inside a field is fine: that fragment is not offered as a handle,
    # and the fields it contributes to the enclosing fragment's own model are
    # collected as conditional, hence optional.
    types = ", ".join(sorted(uncovered))
    return (
        f"Bind at {location}: fragment '{outer}' spreads '{spread}' "
        f"under @skip/@include, and at {types} that is the only way '{spread}' "
        "reaches the slot's root; a fragment readable there is always "
        "requested and always validated, so it cannot be conditional -- drop "
        "the directive, or move the conditional part into its own fragment "
        "that no binding reaches"
    )


@dataclass(kw_only=True, frozen=True)
class _VariableUsage:
    fragment_name: str
    # `graphql.GraphQLType`, not the more specific `GraphQLInputType`: the
    # latter is a `Union[..., GraphQLWrappingType]` with an unparameterized,
    # Unknown-tainted member — see `type_info_input_type`'s header comment in
    # accessors.py.
    input_type: graphql.GraphQLType
    # Whether the schema declares a default for the argument/input field this
    # usage fills — not a default on the variable itself, which a fragment
    # cannot declare. A non-null usage with a location default is still safe
    # to omit: the schema's own default applies when it is.
    has_location_default: bool


class _VariableUsageCollector(graphql.Visitor):
    def __init__(
        self,
        *,
        by_name: dict[str, list[_VariableUsage]],
        type_info: graphql.TypeInfo,
        fragment_name: str,
    ) -> None:
        super().__init__()
        self.by_name = by_name
        self.type_info = type_info
        self.fragment_name = fragment_name

    def enter_variable(self, node: graphql.VariableNode, *_args: object) -> None:
        input_type = type_info_input_type(self.type_info)
        if input_type is None:
            # Not a usable input position (e.g. an unknown argument name):
            # nothing to synthesize here. The variable then has no
            # declaration in the expanded document, and the final
            # `graphql.validate` pass reports it as undefined — the one
            # canonical diagnosis for a malformed usage, not duplicated here.
            return
        self.by_name[node.name.value].append(
            _VariableUsage(
                fragment_name=self.fragment_name,
                input_type=input_type,
                has_location_default=self.type_info.get_default_value()
                is not graphql.Undefined,
            )
        )


def _fragment_variable_usages(
    fragment: graphql.FragmentDefinitionNode, *, schema: graphql.GraphQLSchema
) -> dict[str, list[_VariableUsage]]:
    by_name: dict[str, list[_VariableUsage]] = defaultdict(list)
    type_info = graphql.TypeInfo(schema)
    graphql.visit(
        fragment,
        graphql.TypeInfoVisitor(
            type_info,
            _VariableUsageCollector(
                by_name=by_name,
                type_info=type_info,
                fragment_name=fragment.name.value,
            ),
        ),
    )
    return by_name


def _variable_type_conflict_error(
    *, name: str, entries: list[_VariableUsage], location: str
) -> str | None:
    # A conflict is returned, not raised: `$x` disagreeing with itself says
    # nothing about `$y`, and a developer who wrote both deserves both
    # diagnoses in one regeneration.
    first = entries[0]
    for other in entries[1:]:
        if not graphql.is_equal_type(first.input_type, other.input_type):
            return (
                f"Bind at {location}: variable ${name} is used as "
                f"'{first.input_type}' in fragment '{first.fragment_name}' and "
                f"as '{other.input_type}' in fragment '{other.fragment_name}'; "
                "every usage of a fragment variable must agree on its type"
            )
    return None


def _synthesized_var(*, name: str, entries: list[_VariableUsage]) -> SynthesizedVar:
    # Every usage agrees on the type by the time this runs, so the first one
    # answers for all of them.
    #
    # Omittable is a fact about the positions the variable fills, not about the
    # type the generator ends up declaring: when every one of them declares a
    # schema default, leaving the variable out of the request is what makes
    # that default apply — and that holds whether the position is non-null or
    # nullable. One usage without a default is still a hard requirement,
    # whatever the others allow.
    omittable = all(entry.has_location_default for entry in entries)
    gql_type: graphql.GraphQLType = entries[0].input_type
    # A non-null variable additionally has to be *declared* nullable, or the
    # document is invalid without a value. GraphQL's
    # `VariablesInAllowedPosition` rule is per-position and allows exactly
    # that: a nullable variable at a non-null position that carries a default.
    # A nullable position needs no such relaxation.
    if omittable and isinstance(gql_type, graphql.GraphQLNonNull):
        gql_type = wrapping_of_type(gql_type)
    return SynthesizedVar(
        node=graphql.VariableDefinitionNode(
            variable=graphql.VariableNode(name=graphql.NameNode(value=name)),
            # Round-tripped through the type's own SDL spelling rather than
            # rebuilt wrapper by wrapper: `str(gql_type)` is what graphql-core
            # prints and `parse_type` is its inverse, so the wrapping rules
            # stay the library's to know.
            type=graphql.parse_type(str(gql_type)),
            default_value=None,
            directives=(),
        ),
        omittable=omittable,
    )


def _template_variable_collision_error(
    *,
    name: str,
    template_name: str,
    entries: list[_VariableUsage],
    location: str,
) -> str:
    fragment_names = ", ".join(sorted({entry.fragment_name for entry in entries}))
    return (
        f"Bind at {location}: variable ${name} used in fragment(s) "
        f"'{fragment_names}' has the same name as a variable template "
        f"'{template_name}' already declares; a fragment's owner cannot see "
        "the template's own variable names, so rename the fragment's variable"
    )


def _synthesize_var_defs(
    *,
    template_doc: graphql.DocumentNode,
    operation: graphql.OperationDefinitionNode,
    closure: tuple[graphql.FragmentDefinitionNode, ...],
    schema: graphql.GraphQLSchema,
    template_name: str,
    location: str,
) -> tuple[tuple[SynthesizedVar, ...], list[str]]:
    # A fragment the template document already defines is spread by the
    # template itself, so its variables are the template's own -- declared by
    # the operation and supplied through `execute`. Synthesizing them again
    # would declare them twice; treating them as the fragment's private
    # variables reports a collision with the template that no edit can fix
    # (renaming the fragment's variable breaks the static spread, renaming the
    # template's just moves the clash).
    template_fragment_names = collect_fragments_from_doc(template_doc).keys()
    template_var_names = {
        var_def.variable.name.value for var_def in operation.variable_definitions
    }
    usages: dict[str, list[_VariableUsage]] = defaultdict(list)
    for fragment in closure:
        if fragment.name.value in template_fragment_names:
            continue
        for name, entries in _fragment_variable_usages(fragment, schema=schema).items():
            usages[name].extend(entries)
    # A fragment's variable silently shadowed by the template's own
    # declaration would ship whatever the template's declaration means at
    # that position -- and the fragment's owner, in another module, cannot
    # see the template's variable names to notice the clash. This is a hard
    # error, unlike two *fragments* agreeing on a name below: that merge is
    # intentional.
    errors: list[str] = []
    defined: list[SynthesizedVar] = []
    for name in sorted(usages):
        if name in template_var_names:
            errors.append(
                _template_variable_collision_error(
                    name=name,
                    template_name=template_name,
                    entries=usages[name],
                    location=location,
                )
            )
            continue
        conflict = _variable_type_conflict_error(
            name=name, entries=usages[name], location=location
        )
        if conflict is not None:
            errors.append(conflict)
            continue
        defined.append(_synthesized_var(name=name, entries=usages[name]))
    return tuple(defined), errors


def _build_expanded_document(
    filled: graphql.DocumentNode,
    *,
    fragment_var_defs: tuple[graphql.VariableDefinitionNode, ...],
    closure: tuple[graphql.FragmentDefinitionNode, ...],
    location: str,
) -> graphql.DocumentNode:
    definitions: list[graphql.DefinitionNode] = []
    for definition in filled.definitions:
        if isinstance(definition, graphql.OperationDefinitionNode):
            definitions.append(
                graphql.OperationDefinitionNode(
                    operation=definition.operation,
                    name=definition.name,
                    variable_definitions=(
                        *definition.variable_definitions,
                        *fragment_var_defs,
                    ),
                    directives=definition.directives,
                    selection_set=definition.selection_set,
                )
            )
        else:
            definitions.append(definition)
    # The template document already carries the definitions of every fragment
    # the operation spreads by name, and a bound fragment's closure may reach
    # the very same ones -- so a shared name is usually the same definition,
    # not a conflict, and appending blindly produced a document with two
    # definitions of one fragment that `graphql.validate` rejected as "there
    # can be only one fragment named X", ruling out exactly the brick reuse the
    # README recommends. Merged by name here -- but only when the two sides
    # really are the same definition. A template statement may define a
    # fragment locally (`make_validation_doc` resolves its own spreads
    # local-first), and keeping that copy over a same-named bound statement
    # would ship a document whose `Foo` selects other fields than the `FooData`
    # model the handle validates with.
    #
    # Identity first: brick reuse means the template and the closure usually
    # arrive at the very same node, and printing both sides of that is the bulk
    # of the AST printing a package with shared bricks does.
    by_name = collect_fragments_from_doc(filled)
    errors = [
        _fragment_definition_conflict_error(name=fragment.name.value, location=location)
        for fragment in closure
        if (other := by_name.get(fragment.name.value)) is not None
        and other is not fragment
        and graphql.print_ast(other) != graphql.print_ast(fragment)
    ]
    if errors:
        raise GraphQLGenerationError(errors)
    definitions.extend(
        fragment
        for fragment in sorted(closure, key=lambda fragment: fragment.name.value)
        if fragment.name.value not in by_name
    )
    return graphql.DocumentNode(definitions=definitions)


def _fragment_definition_conflict_error(*, name: str, location: str) -> str:
    return (
        f"Bind at {location} carries fragment '{name}' in its closure, but the "
        f"template defines a different fragment under that name; one name is "
        "one definition in the expanded document -- rename one of them"
    )


def _combination_label(
    *,
    template_name: str,
    spreads: Mapping[str, tuple[graphql.FragmentDefinitionNode, ...]],
) -> str:
    slots = ", ".join(
        f"{key}=[{', '.join(sorted(fragment.name.value for fragment in fragments))}]"
        for key, fragments in sorted(spreads.items())
    )
    return f"{template_name} × {slots}"


def _readable_by_response_key(
    *,
    schema: graphql.GraphQLSchema,
    slots: Mapping[str, SlotTarget],
    direct_by_slot: Mapping[str, tuple[graphql.FragmentDefinitionNode, ...]],
    response_key_of: Mapping[str, str],
    fragments: dict[str, graphql.FragmentDefinitionNode],
    location: str,
) -> tuple[dict[str, tuple[ReadableFragment, ...]], list[str]]:
    # Walks `direct_by_slot`, which is every slot of the template rather than
    # every keyword of the bind, so the result is total over the slots; an
    # unfilled one walks out from no fragment at all and lands on an empty
    # entry.
    #
    # Accumulated across every slot of the bind, like the diagnoses in
    # `collect._expand_binds`: a developer with two broken slots sees both in
    # one regeneration.
    errors: list[str] = []
    by_key: dict[str, tuple[ReadableFragment, ...]] = {}
    for key, direct in sorted(direct_by_slot.items()):
        entries, slot_errors = _readable_fragments(
            schema=schema,
            slot_type=slots[key].type_name,
            direct=direct,
            fragments=fragments,
            location=location,
        )
        errors.extend(slot_errors)
        by_key[response_key_of[key]] = entries
    return dict(sorted(by_key.items())), errors


def expand_binding(
    *,
    schema: graphql.GraphQLSchema,
    template_doc: graphql.DocumentNode,
    # The template document's own operation -- `parser.Query.operation_def`,
    # which is where the rule "the operation is the document's one operation
    # definition" is stated.
    template_operation: graphql.OperationDefinitionNode,
    # The template's name as the rest of the pipeline knows it -- `parser.
    # Query.name`, which falls back to the statement hash for an anonymous
    # operation. Passed rather than read off the operation node, so a
    # diagnosis here quotes the same name the generated class carries.
    template_name: str,
    slots: Mapping[str, SlotTarget],
    spreads: Mapping[str, tuple[graphql.FragmentDefinitionNode, ...]],
    all_fragments: dict[str, graphql.FragmentDefinitionNode],
    location: str,
) -> ExpandedBinding:
    # `slots` and `spreads` are both keyed by the `bind()` keyword; everything
    # this function hands back is keyed by the response key instead. This is
    # the one place that *translates* between the two namespaces -- below this
    # line a slot is only ever its response key. Layers above still handle
    # both (`collect._collect_bindings` reads this output by response key while
    # naming the class by `python_name`), but they never re-derive one from the
    # other.
    #
    # Diagnoses run in phases, and every phase accumulates all of its own
    # before the next one starts. The phase boundaries are barriers by
    # necessity, not by taste: past a bad slot key there is no slot type to
    # read (`_readable_by_response_key` below would KeyError on `slots[key]`),
    # past an unresolvable closure name there is no definition to walk, and
    # past a variable collision the expanded document is *valid* GraphQL that
    # means the wrong thing -- `graphql.validate` would pass it in silence.
    # Within a phase the checks are independent, so stopping at the first one
    # just costs the developer another regeneration.
    #
    # Phase A -- the bind's own arguments against the template's slots.
    _validate_slot_args(schema=schema, slots=slots, spreads=spreads, location=location)
    # The translation itself, written once: everything below keys by the
    # response key, and every one of those keyings reads this map rather than
    # reaching into `slots` again. Built over the slots, so it answers for a
    # slot this bind never mentioned too.
    response_key_of = {key: target.response_key for key, target in slots.items()}
    # The bind's arguments widened to the template's full set of slots: an
    # unfilled slot is an empty tuple, not an absent key. Everything below
    # reads this instead of `spreads`, which is how both maps this function
    # hands on stay total over the slots -- and a key phase A somehow let
    # through survives the widening and crashes on `slots[key]` rather than
    # being silently dropped.
    direct_by_slot: dict[str, tuple[graphql.FragmentDefinitionNode, ...]] = {
        **dict.fromkeys(slots, ()),
        **spreads,
    }

    direct_by_name = {
        fragment.name.value: fragment
        for fragments in spreads.values()
        for fragment in fragments
    }
    # Direct fragments take precedence over the package namespace: they are the
    # exact objects the bind named, and are guaranteed present even if a
    # caller's `all_fragments` snapshot happens not to carry them. Both the
    # document closure and the per-slot readable set resolve names against this
    # one namespace -- the closure needs the union across every slot in this
    # bind call, the readable set works per slot key.
    effective_fragments = {**all_fragments, **direct_by_name}
    # Phase B -- resolving the closure. `collect_transitive_fragment_names`
    # already reports every unresolvable name from one walk, so this phase is
    # a single diagnosis by construction.
    closure = _closure_fragments(
        direct_names=direct_by_name.keys(),
        fragments=effective_fragments,
        location=location,
    )
    # Phase C -- what the resolved closure means. The readable set and the
    # synthesized variables read disjoint parts of it, so their diagnoses are
    # raised together.
    readable_by_response_key, closure_errors = _readable_by_response_key(
        schema=schema,
        slots=slots,
        direct_by_slot=direct_by_slot,
        response_key_of=response_key_of,
        fragments=effective_fragments,
        location=location,
    )
    fragment_vars, var_errors = _synthesize_var_defs(
        template_doc=template_doc,
        operation=template_operation,
        closure=closure,
        schema=schema,
        template_name=template_name,
        location=location,
    )
    closure_errors.extend(var_errors)
    if closure_errors:
        raise GraphQLGenerationError(closure_errors)

    # What the splice writes at each slot: the names the bind itself passed,
    # in the sorted order the document is printed with. The binding reports
    # the same set back as the `direct` entries of `readable_by_response_key`,
    # so no second per-slot list travels downstream.
    names_by_key = {
        response_key_of[key]: tuple(
            sorted(fragment.name.value for fragment in fragments)
        )
        for key, fragments in direct_by_slot.items()
    }
    # Phase D -- assembling the document. Its own conflicts are accumulated
    # inside; a document assembled around one is again valid GraphQL meaning
    # the wrong thing, so phase E never sees it.
    expanded_doc = _build_expanded_document(
        visit_document(template_doc, _SlotFiller(names_by_key=names_by_key)),
        fragment_var_defs=tuple(var.node for var in fragment_vars),
        closure=closure,
        location=location,
    )

    # Phase E -- graphql-core's own accumulator, over a document every earlier
    # phase has passed.
    validation_errors = graphql.validate(schema, expanded_doc)
    if validation_errors:
        combo = _combination_label(template_name=template_name, spreads=spreads)
        raise GraphQLGenerationError([
            f"Bind at {location} ({combo}) is invalid:",
            *(str(error) for error in validation_errors),
        ])

    return ExpandedBinding(
        exec_source=graphql.print_ast(expanded_doc),
        readable_fragments=readable_by_response_key,
        fragment_vars=fragment_vars,
    )
