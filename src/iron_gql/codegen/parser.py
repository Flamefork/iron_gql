import functools
import hashlib
import shutil
import textwrap
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import graphql
import pydantic
from graphql.utilities import value_from_ast_untyped

from iron_gql.codegen.accessors import visit_document
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.slots import QuerySlot
from iron_gql.codegen.slots import build_exec_parts
from iron_gql.codegen.slots import collect_query_slots
from iron_gql.codegen.slots import extend_schema_with_slot
from iron_gql.codegen.slots import has_slot_directive
from iron_gql.codegen.slots import response_key
from iron_gql.codegen.slots import slot_fields
from iron_gql.codegen.slots import spreads_into
from iron_gql.codegen.slots import without_slot_directive
from iron_gql.codegen.util import capitalize_first


@dataclass(kw_only=True, frozen=True)
class Statement:
    raw_text: str
    file: Path
    lineno: int

    @property
    def location(self) -> str:
        return f"{self.file}:{self.lineno}"

    @property
    def clean_text(self) -> str:
        return textwrap.dedent(self.raw_text).strip()

    @property
    def hash_str(self) -> str:
        return hashlib.md5(self.clean_text.encode(), usedforsecurity=False).hexdigest()


@dataclass(kw_only=True, frozen=True)
class GQLVar:
    name: str
    gql_type: graphql.GraphQLType
    default_value: object = graphql.Undefined


def parse_var(
    var_def: graphql.VariableDefinitionNode,
    *,
    schema: graphql.GraphQLSchema,
    context: str = "",
) -> GQLVar:
    var_name = var_def.variable.name.value
    gql_type: graphql.GraphQLType | None = graphql.type_from_ast(  # pyright: ignore[reportUnknownMemberType]
        schema, var_def.type
    )
    if gql_type is None:
        msg = f"Cannot resolve type for ${var_name}"
        if context:
            msg = f"{msg} in {context}"
        raise ValueError(msg)
    default_value: object = graphql.Undefined
    if var_def.default_value is not None:
        # `value_from_ast_untyped` is typed as Any by design
        default_value = value_from_ast_untyped(var_def.default_value)  # pyright: ignore[reportAny]
    return GQLVar(name=var_name, gql_type=gql_type, default_value=default_value)


@dataclass(kw_only=True, frozen=True)
class Query:
    stmt: Statement
    doc: graphql.DocumentNode
    schema: graphql.GraphQLSchema
    fragments: dict[str, graphql.FragmentDefinitionNode]
    # The printed exec source pre-split at each @slot occurrence: the text up
    # to the first gap, then one (slot response key, following text) pair per
    # gap; see `codegen/slots.build_exec_parts`.
    exec_head: str
    exec_splices: tuple[tuple[str, str], ...]

    @functools.cached_property
    def operation_def(self) -> graphql.OperationDefinitionNode:
        for op in self.doc.definitions:
            if isinstance(op, graphql.OperationDefinitionNode):
                return op
        msg = "No operation definition found in the document"
        raise ValueError(msg)

    @property
    def name(self) -> str:
        if self.operation_def.name:
            return self.operation_def.name.value
        return f"query{capitalize_first(self.stmt.hash_str)}"

    @functools.cached_property
    def variables(self) -> list[GQLVar]:
        return [
            parse_var(var_def, schema=self.schema, context=self.stmt.location)
            for var_def in self.operation_def.variable_definitions
        ]

    @property
    def root_type(self) -> graphql.GraphQLObjectType:
        root_type = self.schema.get_root_type(self.operation_def.operation)

        if not root_type:
            msg = f"{self.operation_def.operation} is not defined in the schema"
            raise ValueError(msg)
        return root_type

    @functools.cached_property
    def slots(self) -> tuple[QuerySlot, ...]:
        return collect_query_slots(
            operation_def=self.operation_def,
            schema=self.schema,
            location=self.stmt.location,
        )


@dataclass(kw_only=True, frozen=True)
class FragmentStatement:
    stmt: Statement
    definition: graphql.FragmentDefinitionNode
    schema: graphql.GraphQLSchema
    fragments: dict[str, graphql.FragmentDefinitionNode]

    @property
    def name(self) -> str:
        return self.definition.name.value

    @property
    def type_condition(self) -> str:
        return self.definition.type_condition.name.value

    @functools.cached_property
    def dependencies(self) -> tuple[graphql.FragmentDefinitionNode, ...]:
        names = collect_transitive_fragment_names(
            collect_fragment_spreads(self.definition), self.fragments
        ) - {self.name}
        return tuple(self.fragments[name] for name in sorted(names))

    @functools.cached_property
    def document(self) -> graphql.DocumentNode:
        # The statement standing on its own: its definition plus every
        # definition it transitively spreads, so a name-spread fragment can be
        # validated standalone. A handle never has dependencies — it must be
        # self-contained (see `validate_self_contained_handles`).
        return graphql.DocumentNode(definitions=[self.definition, *self.dependencies])

    @property
    def definition_text(self) -> str:
        # Printed from the AST rather than sliced out of the source: the runtime
        # appends this verbatim to an operation, so the text has to be canonical
        # and independent of how the statement was indented.
        return graphql.print_ast(self.definition)


@dataclass(kw_only=True, frozen=True)
class ParseResult:
    schema: graphql.GraphQLSchema
    queries: list[Query]
    # The single-fragment statements some slot of a validated operation can
    # accept — the ones that become fragment handles. Computed here, once: a
    # statement no slot accepts keeps its pre-slot meaning (spread by name,
    # untyped catch-all at the call site) and is exempt from the
    # standalone-model invariants a handle must satisfy.
    reachable_statements: list[FragmentStatement]
    errors: list[str]


def parse_documents(
    statements: list[Statement],
) -> list[tuple[Statement, graphql.DocumentNode]]:
    return [(stmt, graphql.parse(stmt.clean_text)) for stmt in statements]


def collect_fragments_from_doc(
    doc: graphql.DocumentNode,
) -> dict[str, graphql.FragmentDefinitionNode]:
    fragments: dict[str, graphql.FragmentDefinitionNode] = {}
    for definition in doc.definitions:
        if isinstance(definition, graphql.FragmentDefinitionNode):
            fragments[definition.name.value] = definition
    return fragments


def collect_fragments(
    docs: list[tuple[Statement, graphql.DocumentNode]],
) -> dict[str, graphql.FragmentDefinitionNode]:
    fragments: dict[str, graphql.FragmentDefinitionNode] = {}
    for _, doc in docs:
        fragments.update(collect_fragments_from_doc(doc))
    return fragments


def collect_operations(
    docs: list[tuple[Statement, graphql.DocumentNode]],
) -> list[tuple[Statement, graphql.DocumentNode]]:
    operation_docs: list[tuple[Statement, graphql.DocumentNode]] = []
    for stmt, doc in docs:
        has_operations = any(
            isinstance(d, graphql.OperationDefinitionNode) for d in doc.definitions
        )
        if has_operations:
            operation_docs.append((stmt, doc))
    return operation_docs


def collect_fragment_statements(
    schema: graphql.GraphQLSchema,
    docs: list[tuple[Statement, graphql.DocumentNode]],
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> list[FragmentStatement]:
    statements: list[FragmentStatement] = []
    for stmt, doc in docs:
        # A handle is generated only for a statement that is exactly one
        # fragment: anything else keeps its current meaning (an operation, or a
        # bundle whose fragments are spread statically by name).
        match doc.definitions:
            case [graphql.FragmentDefinitionNode() as definition]:
                statements.append(
                    FragmentStatement(
                        stmt=stmt,
                        definition=definition,
                        schema=schema,
                        fragments=fragments,
                    )
                )
            case _:
                pass
    return statements


def validate_fragment_statements(
    statements: list[FragmentStatement],
) -> tuple[list[FragmentStatement], list[str]]:
    # Returns the statements that validated clean alongside the errors: the
    # rules below spread a fragment into a slot and intersect its possible
    # types, and only a statement that passed here is guaranteed to have a type
    # condition that resolves to a composite type at all.
    #
    # A standalone fragment has no operation to be used by, so the unused rule
    # would reject every one of them; the remaining rules still catch a bad
    # selection, an unknown type condition or a spread cycle.
    rules = tuple(
        rule
        for rule in graphql.specified_rules
        if rule is not graphql.NoUnusedFragmentsRule
    )
    valid: list[FragmentStatement] = []
    errors: list[str] = []
    for statement in statements:
        validation_errors = graphql.validate(
            statement.schema, statement.document, rules=rules
        )
        if validation_errors:
            errors.append(
                f"Invalid GraphQL fragment in {statement.stmt.location}:\n"
                + "\n".join(str(error) for error in validation_errors)
            )
        else:
            valid.append(statement)
    return valid, errors


def collect_variable_names(node: graphql.Node) -> set[str]:
    used: set[str] = set()

    class VariableCollector(graphql.Visitor):
        def enter_variable(self, node: graphql.VariableNode, *_args: object) -> None:
            used.add(node.name.value)

    graphql.visit(node, VariableCollector())
    return used


def slot_compatible_statements(
    schema: graphql.GraphQLSchema,
    statements: list[FragmentStatement],
    slot_types: Iterable[str],
) -> list[FragmentStatement]:
    # A statement that spreads into no slot type inherits no slot base, so
    # no slot kwarg accepts it.
    slot_type_list = list(slot_types)
    return [
        statement
        for statement in statements
        if any(
            spreads_into(schema, statement.type_condition, slot_type)
            for slot_type in slot_type_list
        )
    ]


def _fragment_spellings(
    docs: list[tuple[Statement, graphql.DocumentNode]],
) -> dict[str, dict[str, list[str]]]:
    # Fragment name → printed text → the statement locations spelling it that
    # way; more than one text for a name is the scan-order ambiguity below.
    spellings: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for stmt, doc in docs:
        for definition in doc.definitions:
            if not isinstance(definition, graphql.FragmentDefinitionNode):
                continue
            texts = spellings[definition.name.value]
            texts.setdefault(graphql.print_ast(definition), []).append(stmt.location)
    return spellings


def _ambiguous_locations(
    spellings: dict[str, dict[str, list[str]]], name: str
) -> str | None:
    texts = spellings[name]
    if len(texts) <= 1:
        return None
    return ", ".join(
        sorted(location for locations in texts.values() for location in locations)
    )


def validate_cross_statement_fragments(
    docs: list[tuple[Statement, graphql.DocumentNode]],
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> list[str]:
    # An operation resolves spreads it does not define locally through the
    # global fragment index, where scan order decides which same-named
    # definition wins. Any name crossing a statement boundary must therefore
    # resolve to one canonical text. Bundle-local fragments nobody references
    # from outside stay free to shadow each other: an operation resolves its
    # own spreads local-first via `make_validation_doc`.
    spellings = _fragment_spellings(docs)
    errors: list[str] = []
    reported: set[str] = set()
    for stmt, doc in collect_operations(docs):
        local = collect_fragments_from_doc(doc)
        # The walk overlays the statement's own definitions on the global
        # index, the same way `make_validation_doc` resolves the document that
        # is validated and sent: a locally shadowed name must not pull the
        # global definition's dependencies into the operation's footprint.
        for name in sorted(
            collect_referenced_fragment_names(doc, {**fragments, **local})
        ):
            at = _ambiguous_locations(spellings, name)
            if name in local or at is None or name in reported:
                continue
            reported.add(name)
            message = (
                f"Operation at {stmt.location} spreads fragment '{name}', "
                f"which is defined differently across statements ({at}); a "
                "cross-statement spread must resolve to one canonical "
                "definition"
            )
            errors.append(message)
    return errors


def validate_fragment_variables(statements: list[FragmentStatement]) -> list[str]:
    # A handle ships its text verbatim next to whatever operation it is passed
    # to, and that operation declares no variable on its behalf. Standard
    # validation misses this: a document holding only fragments is valid with
    # any variable reference in it.
    #
    # Only handles that some slot accepts are checked. A fragment nobody can
    # pass into a slot is still spread by name into operations that declare its
    # variables — the ordinary parameterised fragment, which predates slots and
    # must keep generating.
    errors: list[str] = []
    for statement in statements:
        used = collect_variable_names(statement.definition)
        if not used:
            continue
        names = ", ".join(f"${name}" for name in sorted(used))
        message = (
            f"Fragment '{statement.name}' at {statement.stmt.location} cannot "
            f"reference variables ({names}); fragment arguments are not "
            "supported"
        )
        errors.append(message)
    return errors


def validate_self_contained_handles(
    reachable: list[FragmentStatement],
    statements: list[FragmentStatement],
) -> tuple[list[FragmentStatement], list[str]]:
    # A handle travels to the server as its own text alone: shipping transitive
    # definitions next to an arbitrary operation would resolve spreads through
    # the global fragment index, where scan order decides among same-named
    # definitions. A fragment a slot can accept is therefore exactly one
    # self-contained definition; composition by spread stays available to
    # name-spread fragments, which operations resolve and carry themselves.
    #
    # Returns the statements that stay eligible for the combination probe:
    # keeping a spreading handle there would surface its spreads again as
    # unknown fragments, burying this diagnosis.
    errors: list[str] = []
    spreading: set[str] = set()
    for statement in reachable:
        spreads = collect_fragment_spreads(statement.definition)
        if not spreads:
            continue
        spreading.add(statement.name)
        names = ", ".join(f"'{name}'" for name in sorted(spreads))
        message = (
            f"Fragment '{statement.name}' at {statement.stmt.location} spreads "
            f"{names}; a fragment a slot can accept must be self-contained — "
            "inline the spread selections"
        )
        errors.append(message)
    eligible = [
        statement for statement in statements if statement.name not in spreading
    ]
    return eligible, errors


def build_queries(
    schema: graphql.GraphQLSchema,
    docs: list[tuple[Statement, graphql.DocumentNode]],
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> list[Query]:
    queries: list[Query] = []
    for stmt, doc in collect_operations(docs):
        validation_doc = make_validation_doc(doc, fragments)
        exec_head, exec_splices = build_exec_parts(validation_doc)
        queries.append(
            Query(
                stmt=stmt,
                doc=validation_doc,
                schema=schema,
                fragments=collect_fragments_from_doc(validation_doc),
                exec_head=exec_head,
                exec_splices=exec_splices,
            )
        )
    return queries


def validate_queries(queries: list[Query]) -> tuple[list[Query], list[str]]:
    # Partitioned like the fragment statements: the combination rule
    # re-validates a copy of each document once per slot, so letting an
    # already invalid operation through would reprint its errors once per
    # slot and bury the one line that matters.
    valid: list[Query] = []
    errors: list[str] = []
    for query in queries:
        validation_errors = graphql.validate(query.schema, query.doc)
        if validation_errors:
            errors.append(
                f"Invalid GraphQL query in {query.stmt.location}:\n"
                + "\n".join(str(error) for error in validation_errors)
            )
        else:
            valid.append(query)
    return valid, errors


def validate_slots(queries: list[Query]) -> tuple[list[Query], list[str]]:
    # Same partitioning as the fragment statements: `Query.slots` raises on a
    # malformed slot, and the combination rule below reads it for every query
    # it is given, so it may only ever see the ones that resolved.
    valid: list[Query] = []
    errors: list[str] = []
    for query in queries:
        try:
            _ = query.slots
        except GraphQLGenerationError as exc:
            errors.append(str(exc))
        else:
            valid.append(query)
    return valid, errors


def spread_into_slot(
    query: Query,
    slot: QuerySlot,
    statements: tuple[FragmentStatement, ...],
) -> graphql.DocumentNode:
    spreads = tuple(
        graphql.FragmentSpreadNode(name=graphql.NameNode(value=statement.name))
        for statement in statements
    )

    class SpreadInserter(graphql.Visitor):
        def enter_field(self, node: graphql.FieldNode, *_args: object) -> object:
            if not has_slot_directive(node) or response_key(node) != slot.name:
                return None
            selections: list[graphql.SelectionNode] = [
                *(node.selection_set.selections if node.selection_set else []),
                *spreads,
            ]
            return graphql.FieldNode(
                alias=node.alias,
                name=node.name,
                arguments=node.arguments,
                # The probe stands in for the document the runtime assembles and
                # sends, and `@slot` is stripped out of that one — the question
                # being asked here is whether a server would accept it.
                directives=without_slot_directive(node),
                selection_set=graphql.SelectionSetNode(selections=selections),
            )

    doc = visit_document(query.doc, SpreadInserter())
    # Mirrors `build_slot_source`, which appends each handle's definition to an
    # operation that already carries its own: a name the operation itself
    # defines stays duplicated — that document is exactly what the server
    # would reject, so the probe must see it too. Statements, however, are
    # deduplicated by name: the same fragment discovered at several call sites
    # arrives here once per discovery, and whether the texts agree is the
    # dedup pass's own question, not the probe's.
    definitions = list(doc.definitions)
    added: set[str] = set()
    for statement in statements:
        if statement.name in added:
            continue
        added.add(statement.name)
        definitions.append(statement.definition)
    return graphql.DocumentNode(definitions=definitions)


def clashing_definition_names(
    doc: graphql.DocumentNode, statement: FragmentStatement
) -> tuple[str, ...]:
    # `build_slot_source` appends the handle's definition to an operation that
    # already carries every fragment it spreads by name — so a name on both
    # sides is declared twice in the sent query.
    own = {
        definition.name.value
        for definition in doc.definitions
        if isinstance(definition, graphql.FragmentDefinitionNode)
    }
    return tuple(sorted(own & {statement.name}))


def validate_slot_fragment_combinations(
    queries: list[Query], statements: list[FragmentStatement]
) -> list[str]:
    # Every fragment the codebase could pass to this slot is spread into it in
    # one probe, all together: a merge conflict between any two of them is an
    # error even if nobody passes them together, since the operation as
    # written cannot serve both, and graphql-core reports each conflicting
    # pair by fragment name within the single validation. One validation per
    # slot instead of one per pair keeps the probe linear in the number of
    # compatible fragments.
    errors: list[str] = []
    for query in queries:
        for slot in query.slots:
            compatible = slot_compatible_statements(
                query.schema, statements, [slot.type_name]
            )
            spreadable: list[FragmentStatement] = []
            for statement in compatible:
                clashing = clashing_definition_names(query.doc, statement)
                if clashing:
                    errors.append(
                        _definition_clash_error(query, slot, statement, clashing)
                    )
                else:
                    spreadable.append(statement)
            if not spreadable:
                continue
            validation_errors = graphql.validate(
                query.schema, spread_into_slot(query, slot, tuple(spreadable))
            )
            if not validation_errors:
                continue
            names = ", ".join(statement.name for statement in spreadable)
            headline = (
                f"Slot '{slot.name}' at {query.stmt.location} is incompatible "
                f"with {names}:"
            )
            errors.append(
                "\n".join([
                    headline,
                    *(str(error) for error in validation_errors),
                ])
            )
    return errors


def _definition_clash_error(
    query: Query,
    slot: QuerySlot,
    statement: FragmentStatement,
    clashing: tuple[str, ...],
) -> str:
    names = ", ".join(f"'{name}'" for name in clashing)
    return (
        f"Slot '{slot.name}' at {query.stmt.location} cannot accept fragment "
        f"'{statement.name}' at {statement.stmt.location}: the operation already "
        f"defines {names} and the handle ships its own copy of that name, so the "
        "assembled query would declare it twice. A fragment cannot be both "
        "spread into an operation by name and passed into that operation's "
        "slot; rename one of the two."
    )


def validate_no_slots_in_fragments(
    docs: list[tuple[Statement, graphql.DocumentNode]],
) -> list[str]:
    # A fragment can be spread into many queries (or none at all), so a slot
    # marked inside one can't be tied to a single call site the way an
    # operation-level slot can — this is checked across every fragment
    # definition in the project, independent of `build_queries`, which only
    # ever sees documents that contain an operation.
    errors: list[str] = []
    for stmt, doc in docs:
        for definition in doc.definitions:
            if not isinstance(definition, graphql.FragmentDefinitionNode):
                continue
            errors.extend(
                _fragment_slot_error(stmt, definition, node)
                for node in slot_fields(definition)
            )
    return errors


def _fragment_slot_error(
    stmt: Statement,
    definition: graphql.FragmentDefinitionNode,
    node: graphql.FieldNode,
) -> str:
    return (
        f"@slot is only allowed in operations, found on '{node.name.value}' "
        f"in fragment '{definition.name.value}' at {stmt.location}"
    )


def write_debug_artifacts(
    debug_path: Path,
    *,
    schema_path: Path,
    schema_document: graphql.DocumentNode,
    queries: list[Query],
) -> None:
    debug_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(schema_path, debug_path / "schema.graphql")
    dump_strings(
        debug_path / "queries.gql", [query.stmt.clean_text for query in queries]
    )
    # graphql-core's `Node.to_dict` is typed as `Dict[Unknown, Unknown]`;
    # `dump_json` accepts `object`, so we just suppress the leak here.
    dump_json(
        debug_path / "queries.json",
        [query.doc.to_dict() for query in queries],  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    )
    dump_json(
        debug_path / "schema.json",
        schema_document.to_dict(),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    )
    dump_json(
        debug_path / "out.json",
        [
            {
                "stmt": query.stmt.clean_text,
                "location": query.stmt.location,
                "name": query.name,
                "variables": query.variables,
            }
            for query in queries
        ],
    )


def collect_fragment_spreads(node: graphql.Node) -> set[str]:
    spreads: set[str] = set()

    class SpreadCollector(graphql.Visitor):
        def enter_fragment_spread(
            self, node: graphql.FragmentSpreadNode, *_args: object
        ):
            spreads.add(node.name.value)

    graphql.visit(node, SpreadCollector())
    return spreads


def collect_transitive_fragment_names(
    roots: Iterable[str],
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> set[str]:
    visited: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in visited:
            continue
        fragment = fragments.get(name)
        if not fragment:
            continue
        visited.add(name)
        queue.extend(collect_fragment_spreads(fragment))
    return visited


def collect_referenced_fragment_names(
    doc: graphql.DocumentNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> set[str]:
    return collect_transitive_fragment_names(
        [
            spread
            for defn in doc.definitions
            if isinstance(defn, graphql.OperationDefinitionNode)
            for spread in collect_fragment_spreads(defn)
        ],
        fragments,
    )


def make_validation_doc(
    doc: graphql.DocumentNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> graphql.DocumentNode:
    local_fragments = collect_fragments_from_doc(doc)
    defined_fragments = set(local_fragments)
    effective_fragments = {**fragments, **local_fragments}
    referenced_fragments = collect_referenced_fragment_names(doc, effective_fragments)
    extra_definitions = [
        effective_fragments[name]
        for name in sorted(referenced_fragments)
        if name not in defined_fragments
    ]
    return graphql.DocumentNode(definitions=[*doc.definitions, *extra_definitions])


def parse_gql_queries(
    schema_path: Path,
    statements: list[Statement],
    *,
    debug_path: Path | None = None,
) -> ParseResult:
    schema_document = graphql.parse(schema_path.read_text(encoding="utf-8"))
    schema = graphql.build_ast_schema(schema_document)
    schema = extend_schema_with_slot(schema)

    docs = parse_documents(statements)
    fragments = collect_fragments(docs)
    queries = build_queries(schema, docs, fragments)
    valid_statements, fragment_errors = validate_fragment_statements(
        collect_fragment_statements(schema, docs, fragments)
    )
    valid_queries, query_errors = validate_queries(queries)
    # Slot rules read `Query.slots` and re-validate the document, so they run on
    # the operations that already validated: what a slot inside a rejected
    # operation has to say is noise on top of the reason it was rejected.
    slot_queries, slot_errors = validate_slots(valid_queries)
    # Fed the statements that validated: compatibility intersects possible
    # types, which only a resolved composite type condition has.
    reachable_statements = slot_compatible_statements(
        schema,
        valid_statements,
        {slot.type_name for query in slot_queries for slot in query.slots},
    )
    probe_statements, containment_errors = validate_self_contained_handles(
        reachable_statements, valid_statements
    )
    errors = [
        *query_errors,
        *fragment_errors,
        *validate_no_slots_in_fragments(docs),
        *slot_errors,
        *containment_errors,
        *validate_fragment_variables(reachable_statements),
        *validate_cross_statement_fragments(docs, fragments),
        *validate_slot_fragment_combinations(slot_queries, probe_statements),
    ]

    if debug_path:
        write_debug_artifacts(
            debug_path,
            schema_path=schema_path,
            schema_document=schema_document,
            queries=queries,
        )

    return ParseResult(
        schema=schema,
        queries=queries,
        reachable_statements=reachable_statements,
        errors=errors,
    )


def dump_json(path: Path, obj: object) -> None:
    path.write_bytes(
        pydantic.TypeAdapter(type(obj)).dump_json(obj, indent=2, fallback=str)
    )


def dump_strings(path: Path, strings: list[str]) -> None:
    path.write_text("\n\n".join(strings), encoding="utf-8")
