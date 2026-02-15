from __future__ import annotations

import functools
import hashlib
import shutil
import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from pathlib import Path

import graphql
import pydantic
from graphql.utilities import value_from_ast_untyped

from iron_gql.codegen.util import capitalize_first


@dataclass(kw_only=True)
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


@dataclass(kw_only=True)
class Query:
    stmt: Statement
    doc: graphql.DocumentNode
    schema: graphql.GraphQLSchema
    fragments: dict[str, graphql.FragmentDefinitionNode]
    exec_source: str

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
    def variables(self) -> list[GQLVar]:
        return [
            _parse_var(var_def, schema=self.schema, context=self.stmt.location)
            for var_def in self.operation_def.variable_definitions
        ]

    @property
    def root_type(self) -> graphql.GraphQLObjectType:
        root_type = self.schema.get_root_type(self.operation_def.operation)

        if not root_type:
            msg = f"{self.operation_def.operation} is not defined in the schema"
            raise ValueError(msg)
        return root_type


@dataclass(kw_only=True)
class GQLVar:
    name: str
    gql_type: graphql.GraphQLType
    default_value: Any = graphql.Undefined


def _parse_var(
    var_def: graphql.VariableDefinitionNode,
    *,
    schema: graphql.GraphQLSchema,
    context: str = "",
):
    var_name = var_def.variable.name.value
    gql_type = graphql.type_from_ast(schema, var_def.type)
    if gql_type is None:
        msg = f"Cannot resolve type for ${var_name}"
        if context:
            msg = f"{msg} in {context}"
        raise ValueError(msg)
    default_value = graphql.Undefined
    if var_def.default_value is not None:
        default_value = value_from_ast_untyped(var_def.default_value)
    return GQLVar(name=var_name, gql_type=gql_type, default_value=default_value)


@dataclass(kw_only=True)
class ParseResult:
    queries: list[Query]
    error: str | None


def _parse_documents(
    statements: list[Statement],
) -> list[tuple[Statement, graphql.DocumentNode]]:
    return [(stmt, graphql.parse(stmt.clean_text)) for stmt in statements]


def _collect_fragments_from_doc(
    doc: graphql.DocumentNode,
) -> dict[str, graphql.FragmentDefinitionNode]:
    fragments: dict[str, graphql.FragmentDefinitionNode] = {}
    for definition in doc.definitions:
        if isinstance(definition, graphql.FragmentDefinitionNode):
            fragments[definition.name.value] = definition
    return fragments


def _collect_fragments(
    docs: list[tuple[Statement, graphql.DocumentNode]],
) -> dict[str, graphql.FragmentDefinitionNode]:
    fragments: dict[str, graphql.FragmentDefinitionNode] = {}
    for _, doc in docs:
        fragments.update(_collect_fragments_from_doc(doc))
    return fragments


def _collect_operations(
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


def collect_fragment_spreads(node: graphql.Node) -> set[str]:
    spreads: set[str] = set()

    class SpreadCollector(graphql.Visitor):
        def enter_fragment_spread(
            self, node: graphql.FragmentSpreadNode, *_args: object
        ):
            spreads.add(node.name.value)

    graphql.visit(node, SpreadCollector())
    return spreads


def _collect_referenced_fragment_names(
    doc: graphql.DocumentNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> set[str]:
    visited: set[str] = set()
    queue = [
        spread
        for defn in doc.definitions
        if isinstance(defn, graphql.OperationDefinitionNode)
        for spread in collect_fragment_spreads(defn)
    ]
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


def _make_validation_doc(
    doc: graphql.DocumentNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> graphql.DocumentNode:
    local_fragments = _collect_fragments_from_doc(doc)
    defined_fragments = set(local_fragments)
    effective_fragments = {**fragments, **local_fragments}
    referenced_fragments = _collect_referenced_fragment_names(doc, effective_fragments)
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
    """Parse and validate GraphQL queries against a schema.

    Args:
        schema_path: Path to GraphQL schema file (SDL format)
        statements: List of GraphQL query statements to parse
        debug_path: Optional directory to save debug artifacts (schema, queries, AST)

    Returns:
        ParseResult containing validated queries or validation errors
    """
    schema_document = graphql.parse(schema_path.read_text(encoding="utf-8"))
    schema = graphql.build_ast_schema(schema_document)

    docs = _parse_documents(statements)
    fragments = _collect_fragments(docs)
    operation_docs = _collect_operations(docs)

    queries = []
    for stmt, doc in operation_docs:
        validation_doc = _make_validation_doc(doc, fragments)
        exec_source = graphql.print_ast(validation_doc)
        queries.append(
            Query(
                stmt=stmt,
                doc=validation_doc,
                schema=schema,
                fragments=_collect_fragments_from_doc(validation_doc),
                exec_source=exec_source,
            )
        )

    errors = []
    for q in queries:
        errs = graphql.validate(q.schema, q.doc)
        if errs:
            errors.append(
                f"Invalid GraphQL query in {q.stmt.location}:\n"
                + "\n".join(str(e) for e in errs)
            )
            continue

    if debug_path:
        debug_path.mkdir(parents=True, exist_ok=True)
        shutil.copy(schema_path, debug_path / "schema.graphql")
        _dump_strings(debug_path / "queries.gql", [q.stmt.clean_text for q in queries])
        _dump_json(debug_path / "queries.json", [q.doc.to_dict() for q in queries])
        _dump_json(debug_path / "schema.json", schema_document.to_dict())
        _dump_json(
            debug_path / "out.json",
            [
                {
                    "stmt": q.stmt.clean_text,
                    "location": q.stmt.location,
                    "name": q.name,
                    "variables": q.variables,
                }
                for q in queries
            ],
        )

    return ParseResult(queries=queries, error="\n".join(errors) if errors else None)


def _dump_json(path: Path, obj: object):
    path.write_bytes(
        pydantic.TypeAdapter(type(obj)).dump_json(obj, indent=2, fallback=str)
    )


def _dump_strings(path: Path, strings: list[str]):
    path.write_text("\n\n".join(strings), encoding="utf-8")
