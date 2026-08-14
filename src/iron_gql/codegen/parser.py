import functools
import shutil
from collections import defaultdict
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import graphql
import pydantic
from graphql.utilities import value_from_ast_untyped

from iron_gql.codegen.accessors import type_from_ast
from iron_gql.codegen.discovery import BindDecl
from iron_gql.codegen.discovery import BindKeywordCheck
from iron_gql.codegen.discovery import IgnoredBind
from iron_gql.codegen.discovery import SkippedDir
from iron_gql.codegen.discovery import Statement
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.slots import QuerySlot
from iron_gql.codegen.slots import collect_query_slots
from iron_gql.codegen.slots import extend_schema_with_slot
from iron_gql.codegen.slots import slot_fields
from iron_gql.codegen.util import capitalize_first


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
    gql_type = type_from_ast(schema, var_def.type)
    if gql_type is None:
        msg = f"Cannot resolve type for ${var_name}"
        if context:
            msg = f"{msg} in {context}"
        raise ValueError(msg)
    default_value: object = graphql.Undefined
    if var_def.default_value is not None:
        # `value_from_ast_untyped` намеренно типизирован как Any.
        default_value = value_from_ast_untyped(var_def.default_value)  # pyright: ignore[reportAny]
    return GQLVar(name=var_name, gql_type=gql_type, default_value=default_value)


@dataclass(kw_only=True, frozen=True)
class Query:
    # A parsed operation, before it is known which of the two kinds it is.
    # Every query that validates becomes an `Operation` or a `Template` in
    # `classify_queries`; nothing downstream of the parser is handed a bare
    # `Query`, so anything defined here is what both kinds genuinely share.
    stmt: Statement
    doc: graphql.DocumentNode
    schema: graphql.GraphQLSchema
    fragments: dict[str, graphql.FragmentDefinitionNode]

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

    @property
    def class_name(self) -> str:
        return capitalize_first(self.name)

    @property
    def is_subscription(self) -> bool:
        return self.operation_def.operation == graphql.OperationType.SUBSCRIPTION

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


@dataclass(kw_only=True, frozen=True)
class Operation(Query):
    # An operation with no `@slot`: what it sends is the document as written.
    @functools.cached_property
    def exec_source(self) -> str:
        return graphql.print_ast(self.doc)


@dataclass(kw_only=True, frozen=True)
class Template(Query):
    # An operation carrying at least one `@slot`. It is never executed as
    # itself -- each of its bindings prints its own expansion instead (see
    # `bindings.expand_binding`) -- which is why it has no `exec_source`: a
    # template's printed document still carries the `@slot` directive, and
    # sending it would ship that directive to a real server.
    slots: tuple[QuerySlot, ...]


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
        reachable, _unresolvable = collect_transitive_fragment_names(
            collect_fragment_spreads(self.definition), self.fragments
        )
        names = reachable - {self.name}
        return tuple(self.fragments[name] for name in sorted(names))

    @functools.cached_property
    def document(self) -> graphql.DocumentNode:
        # The statement standing on its own: its definition plus every
        # definition it transitively spreads, so a name-spread fragment (and
        # a bind-reachable one, which may itself spread further fragments)
        # can be validated standalone.
        return graphql.DocumentNode(definitions=[self.definition, *self.dependencies])

    @functools.cached_property
    def kind(self) -> Literal["plain", "factory"]:
        # Factory — это fragment, чей document closure (он сам и все
        # transitively spread fragments на любой глубине) использует GraphQL
        # variable. `document` уже является этим closure, поэтому для различия
        # `plain` и `factory` достаточно синтаксически найти `$name`; schema не
        # нужна.
        return "factory" if _uses_a_variable(self.document) else "plain"


@dataclass(kw_only=True, frozen=True)
class ParseResult:
    schema: graphql.GraphQLSchema
    # Classified, so no consumer has to ask `bool(query.slots)` to find out
    # which kind it is holding -- and so the two kinds cannot be handed to a
    # step that means only one of them.
    operations: list[Operation]
    templates: list[Template]
    # Single-fragment statements становятся fragment definitions, если пакет
    # содержит хотя бы один template. Combinations выводятся из
    # schema, поэтому enumeration может достичь fragment без явного `.bind()`.
    # Даже несовместимый ни с одним slot fragment получает class: `bind()`
    # отклоняет его отсутствием overload для on-type base, а не отсутствием
    # type. Пакет без template ничего не bind-ит, и его fragments сохраняют
    # прежнюю семантику: spread по имени и untyped catch-all на call site.
    bindable_statements: list[FragmentStatement]
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
        # Typed definition создаётся только для statement ровно с одним
        # fragment. Operation и bundle сохраняют прежнюю семантику.
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


def build_queries(
    schema: graphql.GraphQLSchema,
    docs: list[tuple[Statement, graphql.DocumentNode]],
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> list[Query]:
    queries: list[Query] = []
    for stmt, doc in collect_operations(docs):
        validation_doc = make_validation_doc(doc, fragments)
        queries.append(
            Query(
                stmt=stmt,
                doc=validation_doc,
                schema=schema,
                fragments=collect_fragments_from_doc(validation_doc),
            )
        )
    return queries


def validate_queries(queries: list[Query]) -> tuple[list[Query], list[str]]:
    # Partitioned like the fragment statements: classification below reads the
    # operation's slots, which is only safe once the document itself is known
    # to be valid GraphQL.
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


def classify_queries(
    queries: list[Query],
) -> tuple[list[Operation], list[Template], list[str]]:
    # The one place the two kinds are told apart: an operation carrying a
    # `@slot` is a template, everything else is a plain operation. Stated once
    # and in the type, so no later step re-derives it from `bool(slots)` and
    # none can be handed the kind it does not mean.
    #
    # Partitioned like the fragment statements: `collect_query_slots` raises
    # on a malformed slot, and a query that fails here becomes neither kind --
    # the failure is reported instead.
    operations: list[Operation] = []
    templates: list[Template] = []
    errors: list[str] = []
    for query in queries:
        try:
            slots = collect_query_slots(
                operation_def=query.operation_def,
                schema=query.schema,
                location=query.stmt.location,
            )
        except GraphQLGenerationError as exc:
            errors.append(str(exc))
            continue
        if slots:
            templates.append(
                Template(
                    stmt=query.stmt,
                    doc=query.doc,
                    schema=query.schema,
                    fragments=query.fragments,
                    slots=slots,
                )
            )
        else:
            operations.append(
                Operation(
                    stmt=query.stmt,
                    doc=query.doc,
                    schema=query.schema,
                    fragments=query.fragments,
                )
            )
    return operations, templates, errors


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
    classified: Sequence[Query],
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
    # `variables` resolves every declaration against the schema and raises on
    # one the schema has no type for -- which is exactly what a document that
    # failed validation may carry. So this dump covers the classified queries,
    # the ones whose declarations are known to resolve; the raw text and AST
    # above already carry every statement, valid or not, which is what a debug
    # run of a *broken* package needs.
    dump_json(
        debug_path / "out.json",
        [
            {
                "stmt": query.stmt.clean_text,
                "location": query.stmt.location,
                "name": query.name,
                "variables": query.variables,
            }
            for query in classified
        ],
    )


def write_ignored_binds(debug_path: Path, ignored: Sequence[IgnoredBind]) -> None:
    # The `.bind(...)` calls discovery resolved and left alone, with the reason
    # each was left. Written by the caller that holds them rather than from
    # `write_debug_artifacts`: they are discovery's output, and `parse_gql_queries`
    # would have to carry them through untouched only to hand them back here.
    # Kept in this module all the same, so the debug directory's layout is
    # decided in one place.
    #
    # An ignored bind is not an error and must not be reported as one -- a
    # third-party `.bind()` is the common case. Here, where only a debug run
    # looks, "ignored on purpose" and "ours and lost" stop being the same empty
    # `binds` list.
    debug_path.mkdir(parents=True, exist_ok=True)
    dump_json(
        debug_path / "ignored_binds.json",
        [{"location": entry.location, "reason": entry.reason} for entry in ignored],
    )


def write_skipped_dirs(debug_path: Path, skipped: Sequence[SkippedDir]) -> None:
    # The directories the walk refused to enter, with the reason each was
    # refused. Here for the same reason `ignored_binds.json` is: a tree left
    # alone on purpose and a tree the scan lost both show up as statements that
    # are not in the package, and only this file tells them apart.
    #
    # The path and the reason, and nothing about the contents: counting the
    # `.py` files under a refused directory to make the report richer is the
    # very walk this refusal exists to avoid.
    debug_path.mkdir(parents=True, exist_ok=True)
    dump_json(
        debug_path / "skipped_dirs.json",
        [{"location": entry.location, "reason": entry.reason} for entry in skipped],
    )


class _SpreadCollector(graphql.Visitor):
    def __init__(self, spreads: set[str]) -> None:
        super().__init__()
        self.spreads = spreads

    def enter_fragment_spread(
        self, node: graphql.FragmentSpreadNode, *_args: object
    ) -> None:
        self.spreads.add(node.name.value)


class _VariablePresenceCollector(graphql.Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.found = False

    def enter_variable(self, _node: graphql.VariableNode, *_args: object) -> None:
        self.found = True


def _uses_a_variable(doc: graphql.DocumentNode) -> bool:
    # Presence only, no schema: `bindings._fragment_variable_usages` resolves
    # each usage's type and default-ness for `with_args`'s own signature, but
    # classifying "plain" from "factory" needs none of that, only whether a
    # `$name` appears anywhere in the closure at all.
    visitor = _VariablePresenceCollector()
    graphql.visit(doc, visitor)
    return visitor.found


def collect_fragment_spreads(node: graphql.Node) -> set[str]:
    spreads: set[str] = set()
    graphql.visit(node, _SpreadCollector(spreads))
    return spreads


def collect_transitive_fragment_names(
    roots: Iterable[str],
    fragments: Mapping[str, graphql.FragmentDefinitionNode],
) -> tuple[set[str], set[str]]:
    # (resolved, unresolvable) from one walk. Callers that only need the
    # reachable set discard the second half -- `graphql.validate` is the one
    # place a spread with no definition is diagnosed -- while `bindings`
    # reports the unresolvable names itself, because a bind can only see
    # single-fragment statements and has its own remedy to offer.
    visited: set[str] = set()
    missing: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in visited:
            continue
        fragment = fragments.get(name)
        if not fragment:
            missing.add(name)
            continue
        visited.add(name)
        queue.extend(collect_fragment_spreads(fragment))
    return visited, missing


def collect_referenced_fragment_names(
    doc: graphql.DocumentNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> set[str]:
    reachable, _unresolvable = collect_transitive_fragment_names(
        [
            spread
            for defn in doc.definitions
            if isinstance(defn, graphql.OperationDefinitionNode)
            for spread in collect_fragment_spreads(defn)
        ],
        fragments,
    )
    return reachable


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


def bindable_statements(
    templates: list[Template],
    statements: list[FragmentStatement],
) -> tuple[list[FragmentStatement], list[str]]:
    # При наличии template каждый single-fragment statement становится
    # bindable — см. `ParseResult.bindable_statements`.
    #
    # Spread closure каждого definition обходится только по таким statements:
    # именно их видит bind. Fragment внутри multi-definition statement доступен
    # для spread по имени, но не может стать bindable definition, поэтому ошибка
    # разрешения диагностируется здесь. `expand_binding` затем обходит тот же
    # closure по возвращённому набору. Обход идёт по statements, а не по bind,
    # поэтому fragment без явного `.bind()` всё равно участвует в enumeration.
    if not templates:
        return [], []
    bindable_definitions = {
        statement.name: statement.definition for statement in statements
    }
    errors: list[str] = []
    for statement in statements:
        _names, missing = collect_transitive_fragment_names(
            {statement.name}, bindable_definitions
        )
        if missing:
            names = ", ".join(sorted(missing))
            msg = (
                f"Fragment '{statement.name}' at {statement.stmt.location} "
                f"spreads fragment(s) {names} in its closure, but they are not "
                "single-fragment statements a bind can see -- split each into "
                "its own statement"
            )
            errors.append(msg)
    return statements, errors


def validate_bind_templates(
    binds: Sequence[BindDecl | BindKeywordCheck],
    all_queries: list[Query],
    operations: list[Operation],
    templates: list[Template],
) -> list[str]:
    # Three sets on purpose. `all_queries` is unfiltered, and answers "is this
    # even operation-shaped" -- safe to ask of a query that never validated.
    # The other two are what classification produced, so membership in either
    # *is* the answer to "did it validate", and which one it landed in *is*
    # the answer to "does it have slots" -- neither is re-derived here.
    all_stmts = {query.stmt for query in all_queries}
    template_stmts = {query.stmt for query in templates}
    operation_by_stmt = {query.stmt: query for query in operations}
    errors: list[str] = []
    for bind in binds:
        if bind.template not in all_stmts:
            message = (
                f"Bind at {bind.location}: template at {bind.template.location} "
                "is not an operation"
            )
            errors.append(message)
        elif bind.template in template_stmts:
            continue
        elif (operation := operation_by_stmt.get(bind.template)) is not None:
            message = (
                f"Bind at {bind.location}: template '{operation.name}' at "
                f"{bind.template.location} has no slots"
            )
            errors.append(message)
        else:
            # It IS an operation, so "is not an operation" would be wrong —
            # it just never validated; that failure is already reported on
            # its own as a GraphQL or slot error elsewhere in this same list.
            message = (
                f"Bind at {bind.location}: template at {bind.template.location} "
                "failed to validate; see the GraphQL error reported for it"
            )
            errors.append(message)
    return errors


def validate_bind_slot_args(
    binds: Sequence[BindDecl],
    statements: list[FragmentStatement],
) -> list[str]:
    by_stmt = {statement.stmt: statement for statement in statements}
    errors: list[str] = []
    for bind in binds:
        for key, stmts in bind.slot_args:
            for stmt in stmts:
                if stmt in by_stmt:
                    continue
                message = (
                    f"Bind at {bind.location}: value for slot '{key}' at "
                    f"{stmt.location} is not a single-fragment statement"
                )
                errors.append(message)
    return errors


def parse_gql_queries(
    schema_path: Path,
    statements: list[Statement],
    binds: Sequence[BindDecl],
    *,
    bind_keyword_checks: Sequence[BindKeywordCheck],
    debug_path: Path | None = None,
) -> ParseResult:
    schema_document = graphql.parse(schema_path.read_text(encoding="utf-8"))
    schema = graphql.build_ast_schema(schema_document)
    schema = extend_schema_with_slot(schema)

    docs = parse_documents(statements)
    fragments = collect_fragments(docs)
    queries = build_queries(schema, docs, fragments)
    all_fragment_statements = collect_fragment_statements(schema, docs, fragments)
    valid_statements, fragment_errors = validate_fragment_statements(
        all_fragment_statements
    )
    valid_queries, query_errors = validate_queries(queries)
    # Classification reads the operation's slots and re-validates the document,
    # so it runs on the operations that already validated: what a slot inside a
    # rejected operation has to say is noise on top of the reason it was
    # rejected.
    operations, templates, slot_errors = classify_queries(valid_queries)
    # A statement's bindability doesn't depend on whether it validated
    # cleanly, but everything downstream (`expand_binding`, the IR collector)
    # only ever sees statements that did.
    bindable, closure_errors = bindable_statements(templates, valid_statements)

    if debug_path:
        write_debug_artifacts(
            debug_path,
            schema_path=schema_path,
            schema_document=schema_document,
            queries=queries,
            classified=[*operations, *templates],
        )

    return ParseResult(
        schema=schema,
        operations=operations,
        templates=templates,
        bindable_statements=bindable,
        errors=[
            *query_errors,
            *fragment_errors,
            *validate_no_slots_in_fragments(docs),
            *slot_errors,
            *validate_cross_statement_fragments(docs, fragments),
            *validate_bind_templates(
                [*binds, *bind_keyword_checks], queries, operations, templates
            ),
            *validate_bind_slot_args(binds, all_fragment_statements),
            *closure_errors,
        ],
    )


def dump_json(path: Path, obj: object) -> None:
    path.write_bytes(
        pydantic.TypeAdapter(type(obj)).dump_json(obj, indent=2, fallback=str)
    )


def dump_strings(path: Path, strings: list[str]) -> None:
    path.write_text("\n\n".join(strings), encoding="utf-8")
