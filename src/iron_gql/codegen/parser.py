import functools
import shutil
from collections import defaultdict
from collections.abc import Collection
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path
from typing import Literal

import graphql
import pydantic
from graphql.utilities import value_from_ast_untyped
from graphql.validation.validate import validate_sdl

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


@dataclass(kw_only=True, frozen=True)
class _ValidatedOperation:
    # What an operation and a template share: a statement holding exactly one
    # operation, whose document -- the statement's own definitions plus every
    # fragment they spread from elsewhere in the package -- passed validation.
    # Only `parse_operation_statement` builds one, after that validation, so
    # every fact below is resolved once there and never read off an unchecked
    # document.
    stmt: Statement
    doc: graphql.DocumentNode
    operation_def: graphql.OperationDefinitionNode
    root_type: graphql.GraphQLObjectType
    variables: tuple[GQLVar, ...]
    fragments: dict[str, graphql.FragmentDefinitionNode]

    @property
    def name(self) -> str:
        if self.operation_def.name:
            return self.operation_def.name.value
        # ADR 0004: one base for every kind -- the class's suffix already names
        # the kind, and `anonymous` says where the hash comes from.
        return f"anonymous{capitalize_first(self.stmt.hash_str)}"

    @property
    def class_name(self) -> str:
        # ADR 0004: the kind suffix keeps a developer-written name out of the
        # names the module's shapes claim -- `query Post` next to a model of
        # type `Post`. Applied without exceptions, `query GetUserQuery`
        # included: an exception would make the class name depend on how the
        # operation happens to be spelled.
        kind = capitalize_first(self.operation_def.operation.value)
        return f"{capitalize_first(self.name)}{kind}"

    @property
    def is_subscription(self) -> bool:
        return self.operation_def.operation == graphql.OperationType.SUBSCRIPTION


@dataclass(kw_only=True, frozen=True)
class Operation(_ValidatedOperation):
    # An operation with no `@slot`: what it sends is the document as written.
    @functools.cached_property
    def exec_source(self) -> str:
        return graphql.print_ast(self.doc)


@dataclass(kw_only=True, frozen=True)
class Template(_ValidatedOperation):
    # An operation carrying at least one `@slot`. It is never executed as
    # itself -- each of its bindings prints its own expansion instead (see
    # `bindings.expand_binding`) -- which is why it has no `exec_source`: a
    # template's printed document still carries the `@slot` directive, and
    # sending it would ship that directive to a real server.
    slots: tuple[QuerySlot, ...]


type CompositeType = (
    graphql.GraphQLObjectType | graphql.GraphQLInterfaceType | graphql.GraphQLUnionType
)


@dataclass(kw_only=True, frozen=True)
class FragmentStatement:
    # A statement holding exactly one fragment definition, built only once its
    # document closure has validated: the type condition is resolved and the
    # closure is the one that validated, so nothing downstream resolves either
    # again.
    stmt: Statement
    definition: graphql.FragmentDefinitionNode
    type_condition: CompositeType
    # Every definition the fragment spreads, at any depth, sorted by name.
    dependencies: tuple[graphql.FragmentDefinitionNode, ...]

    @property
    def name(self) -> str:
        return self.definition.name.value

    @property
    def fragments(self) -> dict[str, graphql.FragmentDefinitionNode]:
        return {
            definition.name.value: definition
            for definition in (self.definition, *self.dependencies)
        }

    @property
    def document(self) -> graphql.DocumentNode:
        # The statement standing on its own: its definition plus every
        # definition it transitively spreads -- what was validated, and what
        # has to be sent with it.
        return graphql.DocumentNode(definitions=(self.definition, *self.dependencies))

    @functools.cached_property
    def kind(self) -> Literal["plain", "factory"]:
        # A factory is a fragment whose document closure (the fragment itself
        # and all transitively spread fragments at any depth) uses a GraphQL
        # variable. `document` already is that closure, so finding `$name`
        # syntactically is enough to tell `plain` from `factory`; no schema is
        # needed.
        return "factory" if _uses_a_variable(self.document) else "plain"


@dataclass(kw_only=True, frozen=True)
class ParseResult:
    schema: graphql.GraphQLSchema
    # Classified, so no consumer has to ask `bool(query.slots)` to find out
    # which kind it is holding -- and so the two kinds cannot be handed to a
    # step that means only one of them.
    operations: list[Operation]
    templates: list[Template]
    # Single-fragment statements become fragment definitions if the package
    # contains at least one template. Combinations are derived from the schema,
    # so enumeration can reach a fragment without an explicit `.bind()`. Even a
    # fragment compatible with no slot gets a class: `bind()` rejects it by
    # having no overload for its on-type base, not by a missing type. A package
    # without a template binds nothing, and its fragments keep the old
    # semantics: spread by name and the untyped catch-all at the call site.
    bindable_statements: list[FragmentStatement]
    errors: list[str]


# Every statement is parsed before any rejects: one run reports each syntax
# error. They are reported alone, without the diagnoses of the rest of the
# input -- a fragment that failed to parse turns every spread of it into an
# unknown-fragment error that is only noise. The cause is the one syntax error,
# or a group of them when there are several.
def parse_documents(
    statements: list[Statement],
) -> list[tuple[Statement, graphql.DocumentNode]]:
    docs: list[tuple[Statement, graphql.DocumentNode]] = []
    failures: list[tuple[Statement, graphql.GraphQLSyntaxError]] = []
    for stmt in statements:
        try:
            docs.append((stmt, graphql.parse(stmt.clean_text)))
        except graphql.GraphQLSyntaxError as exc:
            failures.append((stmt, exc))
    if not failures:
        return docs
    errors = list(starmap(_statement_syntax_error, failures))
    match [exc for _, exc in failures]:
        case [exc]:
            raise GraphQLGenerationError(errors) from exc
        case excs:
            raise GraphQLGenerationError(errors) from ExceptionGroup(
                "statement syntax errors", excs
            )


def _statement_syntax_error(stmt: Statement, exc: graphql.GraphQLSyntaxError) -> str:
    match exc.locations:
        case [location]:
            return (
                f"{stmt.location}: {exc.message} (line {location.line}, "
                f"column {location.column} of the dedented statement)"
            )
        case _:
            # Internal invariant: graphql-core raises a syntax error at the one
            # token it stopped on.
            msg = f"syntax error without a single location: {exc.locations}"
            raise AssertionError(msg)


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
    # The package's one fragment index: every spread, wherever it stands,
    # resolves through it. Which of several same-named definitions lands here
    # does not matter -- `validate_fragment_names` rejects any two that differ.
    fragments: dict[str, graphql.FragmentDefinitionNode] = {}
    for _, doc in docs:
        fragments.update(collect_fragments_from_doc(doc))
    return fragments


def validate_fragment_names(
    docs: list[tuple[Statement, graphql.DocumentNode]],
) -> list[str]:
    # ADR 0006: a fragment name means one definition across the package, in
    # every kind of statement. Two definitions are the same when they print
    # the same -- printing drops whitespace, line breaks and comments.
    texts: dict[str, set[str]] = defaultdict(set)
    locations: dict[str, dict[str, None]] = defaultdict(dict)
    for stmt, doc in docs:
        for definition in doc.definitions:
            if not isinstance(definition, graphql.FragmentDefinitionNode):
                continue
            texts[definition.name.value].add(graphql.print_ast(definition))
            locations[definition.name.value][stmt.location] = None
    return [
        (
            f"Fragment '{name}' is defined differently at "
            f"{', '.join(locations[name])}; a fragment name means one definition "
            "across the package -- rename one of the fragments or make their "
            "definitions agree"
        )
        for name, spellings in texts.items()
        if len(spellings) > 1
    ]


def _external_fragments(
    doc: graphql.DocumentNode,
    fragments: Mapping[str, graphql.FragmentDefinitionNode],
) -> tuple[graphql.FragmentDefinitionNode, ...]:
    # Every definition the statement's own definitions spread, at any depth,
    # that the statement does not define itself, sorted by name: what has to
    # be validated and sent with it. The walk reads the statement's own
    # definitions where it has them, as the document will: with one definition
    # per name they are the index's, and when `validate_fragment_names` finds
    # they are not, its diagnosis is not buried under what the other
    # definition would spread. A name the index lacks is left out --
    # validation is what reports it.
    own = collect_fragments_from_doc(doc)
    reachable = collect_transitive_fragment_names(
        {
            name
            for definition in doc.definitions
            for name in collect_fragment_spreads(definition)
        },
        {**fragments, **own},
    )
    return tuple(fragments[name] for name in sorted(reachable - own.keys()))


def _with_external_fragments(
    doc: graphql.DocumentNode, external: tuple[graphql.FragmentDefinitionNode, ...]
) -> graphql.DocumentNode:
    return graphql.DocumentNode(definitions=(*doc.definitions, *external))


def _operation_defs(
    doc: graphql.DocumentNode,
) -> list[graphql.OperationDefinitionNode]:
    return [
        definition
        for definition in doc.definitions
        if isinstance(definition, graphql.OperationDefinitionNode)
    ]


def _single_fragment(
    doc: graphql.DocumentNode,
) -> graphql.FragmentDefinitionNode | None:
    definitions = doc.definitions
    if len(definitions) == 1 and isinstance(
        definition := definitions[0], graphql.FragmentDefinitionNode
    ):
        return definition
    return None


# A standalone fragment has no operation to be used by, so the unused rule
# would reject every one of them; the remaining rules still catch a bad
# selection, an unknown type condition or a spread cycle.
_STANDALONE_FRAGMENT_RULES = tuple(
    rule
    for rule in graphql.specified_rules
    if rule is not graphql.NoUnusedFragmentsRule
)


def _parse_statement(
    stmt: Statement,
    doc: graphql.DocumentNode,
    *,
    external: tuple[graphql.FragmentDefinitionNode, ...],
    schema: graphql.GraphQLSchema,
) -> Operation | Template | FragmentStatement | None:
    # The one step from a statement to what it means: an operation or a
    # template, a fragment definition, or -- for a statement with several
    # fragment definitions -- nothing of its own, its fragments only spread by
    # name. Each is built after its document validated, with every fact
    # resolved here once; a statement that fails raises its diagnoses.
    document = _with_external_fragments(doc, external)
    match _operation_defs(doc):
        case []:
            # Validated even when nothing spreads it: a statement with several
            # definitions is otherwise never checked at all.
            rule_errors = graphql.validate(
                schema, document, rules=_STANDALONE_FRAGMENT_RULES
            )
            if rule_errors:
                raise GraphQLGenerationError([
                    _validation_error("fragment", stmt, rule_errors)
                ])
            match _single_fragment(doc):
                case None:
                    return None
                case definition:
                    return FragmentStatement(
                        stmt=stmt,
                        definition=definition,
                        type_condition=_type_condition(schema, definition),
                        dependencies=external,
                    )
        case [operation_def]:
            return _operation(stmt, document, operation_def, schema=schema)
        case operation_defs:
            msg = (
                f"{stmt.location}: the statement holds {len(operation_defs)} "
                "operations, but a gql call takes one -- split it into one call "
                "per operation"
            )
            raise GraphQLGenerationError([msg])


def _validation_error(
    kind: str, stmt: Statement, errors: list[graphql.GraphQLError]
) -> str:
    return f"Invalid GraphQL {kind} in {stmt.location}:\n" + "\n".join(
        str(error) for error in errors
    )


def _operation(
    stmt: Statement,
    document: graphql.DocumentNode,
    operation_def: graphql.OperationDefinitionNode,
    *,
    schema: graphql.GraphQLSchema,
) -> Operation | Template:
    if rule_errors := graphql.validate(schema, document):
        raise GraphQLGenerationError([_validation_error("query", stmt, rule_errors)])
    # The one place the two kinds are told apart: an operation carrying a
    # `@slot` is a template, everything else is a plain operation. Stated once
    # and in the type, so no later step re-derives it from `bool(slots)` and
    # none can be handed the kind it does not mean. Read only once the document
    # validated: what a slot inside a rejected operation has to say is noise on
    # top of the reason it was rejected.
    slots = collect_query_slots(
        operation_def=operation_def, schema=schema, location=stmt.location
    )
    root_type = _root_type(schema, operation_def)
    variables = tuple(
        _variable(schema, var_def)
        for var_def in operation_def.variable_definitions or ()
    )
    fragments = collect_fragments_from_doc(document)
    if slots:
        return Template(
            stmt=stmt,
            doc=document,
            operation_def=operation_def,
            root_type=root_type,
            variables=variables,
            fragments=fragments,
            slots=slots,
        )
    return Operation(
        stmt=stmt,
        doc=document,
        operation_def=operation_def,
        root_type=root_type,
        variables=variables,
        fragments=fragments,
    )


def _root_type(
    schema: graphql.GraphQLSchema, operation_def: graphql.OperationDefinitionNode
) -> graphql.GraphQLObjectType:
    match schema.get_root_type(operation_def.operation):
        case graphql.GraphQLObjectType() as root_type:
            return root_type
        case _:
            # Internal invariant: `KnownOperationTypesRule` rejects an
            # operation whose root the schema does not define.
            msg = f"{operation_def.operation.value} is not defined in the schema"
            raise AssertionError(msg)


def _variable(
    schema: graphql.GraphQLSchema, var_def: graphql.VariableDefinitionNode
) -> GQLVar:
    var_name = var_def.variable.name.value
    gql_type = type_from_ast(schema, var_def.type)
    if gql_type is None:
        # Internal invariant: `KnownTypeNamesRule` rejects a variable whose type
        # the schema does not define.
        msg = f"cannot resolve the type of ${var_name}"
        raise AssertionError(msg)
    default_value: object = graphql.Undefined
    if var_def.default_value is not None:
        # `value_from_ast_untyped` is deliberately typed as Any.
        default_value = value_from_ast_untyped(var_def.default_value)  # pyright: ignore[reportAny]
    return GQLVar(name=var_name, gql_type=gql_type, default_value=default_value)


def _type_condition(
    schema: graphql.GraphQLSchema, definition: graphql.FragmentDefinitionNode
) -> CompositeType:
    match schema.get_type(definition.type_condition.name.value):
        case (
            graphql.GraphQLObjectType()
            | graphql.GraphQLInterfaceType()
            | graphql.GraphQLUnionType()
        ) as type_condition:
            return type_condition
        case other:
            # Internal invariant: `KnownTypeNamesRule` and
            # `FragmentsOnCompositeTypesRule` reject any other type condition.
            msg = f"fragment {definition.name.value} on {other}"
            raise AssertionError(msg)


def validate_no_slots_in_fragments(
    docs: list[tuple[Statement, graphql.DocumentNode]],
) -> list[str]:
    # A fragment can be spread into many queries (or none at all), so a slot
    # marked inside one can't be tied to a single call site the way an
    # operation-level slot can — this is checked across every fragment
    # definition in the project, whichever kind of statement holds it.
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
    queries: list[tuple[Statement, graphql.DocumentNode]],
    classified: Sequence[Operation | Template],
) -> None:
    # `queries` is every statement holding an operation, valid or not, with
    # the fragments it spreads from elsewhere appended -- what a debug run of a
    # *broken* module needs; `out.json` covers only what was built from them.
    debug_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(schema_path, debug_path / "schema.graphql")
    dump_strings(debug_path / "queries.gql", [stmt.clean_text for stmt, _ in queries])
    # graphql-core's `Node.to_dict` is typed as `Dict[Unknown, Unknown]`;
    # `dump_json` accepts `object`, so we just suppress the leak here.
    dump_json(
        debug_path / "queries.json",
        [doc.to_dict() for _, doc in queries],  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
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
    # are not in the module, and only this file tells them apart.
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
) -> set[str]:
    # A name with no definition is skipped: `graphql.validate` is the one place
    # a spread with no definition is diagnosed.
    visited: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in visited or name not in fragments:
            continue
        visited.add(name)
        queue.extend(collect_fragment_spreads(fragments[name]))
    return visited


def bindable_statements(
    templates: list[Template],
    statements: list[FragmentStatement],
) -> tuple[list[FragmentStatement], list[str]]:
    # When a template is present, every single-fragment statement becomes
    # bindable -- see `ParseResult.bindable_statements`.
    #
    # A bind sees single-fragment statements only. A fragment inside a
    # statement with several definitions is available for spread by name but
    # cannot become a bindable definition, so a closure reaching one is
    # diagnosed here, once: `expand_binding` takes the returned statements'
    # closures as they are. Checked over statements, not binds, so a fragment
    # without an explicit `.bind()` still takes part in enumeration.
    if not templates:
        return [], []
    bindable_names = {statement.name for statement in statements}
    errors: list[str] = []
    for statement in statements:
        missing = {
            dependency.name.value for dependency in statement.dependencies
        } - bindable_names
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
    operation_stmts: Collection[Statement],
    operations: list[Operation],
    templates: list[Template],
) -> list[str]:
    # Three sets on purpose. `operation_stmts` is every statement holding an
    # operation, and answers "is this even operation-shaped" -- built or not.
    # The other two are what parsing built, so membership in either *is* the
    # answer to "did it pass", and which one it landed in *is* the answer to
    # "does it have slots" -- neither is re-derived here.
    template_stmts = {query.stmt for query in templates}
    operation_by_stmt = {query.stmt: query for query in operations}
    errors: list[str] = []
    for bind in binds:
        if bind.template not in operation_stmts:
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
            # it just never passed; that failure is already reported on its
            # own elsewhere in this same list.
            message = (
                f"Bind at {bind.location}: template at {bind.template.location} "
                "failed to validate; see the error reported for it"
            )
            errors.append(message)
    return errors


def validate_bind_slot_args(
    binds: Sequence[BindDecl],
    fragment_stmts: Collection[Statement],
) -> list[str]:
    # `fragment_stmts` is every statement holding a single fragment, valid or
    # not: an invalid one is answered by its own validation error.
    errors: list[str] = []
    for bind in binds:
        for key, stmts in bind.slot_args:
            for stmt in stmts:
                if stmt in fragment_stmts:
                    continue
                message = (
                    f"Bind at {bind.location}: value for slot '{key}' at "
                    f"{stmt.location} is not a single-fragment statement"
                )
                errors.append(message)
    return errors


# graphql-core reports an invalid SDL or schema as a TypeError, the type a
# defect of the generator raises too; asked for its diagnoses as a list, it
# tells the two apart.
def load_schema(
    schema_path: Path,
) -> tuple[graphql.DocumentNode, graphql.GraphQLSchema]:
    try:
        text = schema_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        msg = (
            f"Schema file {schema_path} does not exist; "
            "pass schema_path pointing at the schema SDL"
        )
        raise GraphQLGenerationError([msg]) from exc
    try:
        document = graphql.parse(text)
    except graphql.GraphQLSyntaxError as exc:
        raise GraphQLGenerationError([_schema_error(schema_path, exc)]) from exc
    if sdl_errors := validate_sdl(document):
        raise GraphQLGenerationError([
            _schema_error(schema_path, error) for error in sdl_errors
        ])
    schema = graphql.build_ast_schema(document, assume_valid_sdl=True)
    if schema_errors := graphql.validate_schema(schema):
        raise GraphQLGenerationError([
            _schema_error(schema_path, error) for error in schema_errors
        ])
    return document, schema


def _schema_error(schema_path: Path, error: graphql.GraphQLError) -> str:
    match error.locations:
        case None | []:
            place = str(schema_path)
        case locations:
            place = ", ".join(
                f"{schema_path}:{location.line}:{location.column}"
                for location in locations
            )
    return f"Invalid schema at {place}: {error.message}"


def parse_gql_queries(
    schema_path: Path,
    statements: list[Statement],
    binds: Sequence[BindDecl],
    *,
    bind_keyword_checks: Sequence[BindKeywordCheck],
    debug_path: Path | None = None,
) -> ParseResult:
    schema_document, schema = load_schema(schema_path)
    schema = extend_schema_with_slot(schema)

    docs = parse_documents(statements)
    fragments = collect_fragments(docs)
    # The statements as written, kept apart from what is built from them:
    # whether a statement holds an operation is asked of every one of them,
    # built or not -- by the debug dump and by the bind diagnoses.
    operation_sources: list[tuple[Statement, graphql.DocumentNode]] = []
    operations: list[Operation] = []
    templates: list[Template] = []
    fragment_statements: list[FragmentStatement] = []
    statement_errors: list[str] = []
    for stmt, doc in docs:
        external = _external_fragments(doc, fragments)
        if _operation_defs(doc):
            operation_sources.append((stmt, _with_external_fragments(doc, external)))
        try:
            parsed = _parse_statement(stmt, doc, external=external, schema=schema)
        except GraphQLGenerationError as exc:
            statement_errors.extend(exc.errors)
            continue
        match parsed:
            case Operation():
                operations.append(parsed)
            case Template():
                templates.append(parsed)
            case FragmentStatement():
                fragment_statements.append(parsed)
            case None:
                pass
    bindable, closure_errors = bindable_statements(templates, fragment_statements)

    if debug_path:
        write_debug_artifacts(
            debug_path,
            schema_path=schema_path,
            schema_document=schema_document,
            queries=operation_sources,
            classified=[*operations, *templates],
        )

    return ParseResult(
        schema=schema,
        operations=operations,
        templates=templates,
        bindable_statements=bindable,
        errors=[
            *statement_errors,
            *validate_fragment_names(docs),
            *validate_no_slots_in_fragments(docs),
            *validate_bind_templates(
                [*binds, *bind_keyword_checks],
                {stmt for stmt, _ in operation_sources},
                operations,
                templates,
            ),
            *validate_bind_slot_args(
                binds, {stmt for stmt, doc in docs if _single_fragment(doc)}
            ),
            *closure_errors,
        ],
    )


def dump_json(path: Path, obj: object) -> None:
    path.write_bytes(
        pydantic.TypeAdapter(type(obj)).dump_json(obj, indent=2, fallback=str)
    )


def dump_strings(path: Path, strings: list[str]) -> None:
    path.write_text("\n\n".join(strings), encoding="utf-8")
