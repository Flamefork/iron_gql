import functools
import hashlib
import shutil
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import cast

import graphql
import pydantic
from graphql.utilities import value_from_ast_untyped

from iron_gql.util import capitalize_first


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

    @functools.cached_property
    def _operation_def(self) -> graphql.OperationDefinitionNode:
        for op in self.doc.definitions:
            if isinstance(op, graphql.OperationDefinitionNode):
                return op
        msg = "No operation definition found in the document"
        raise ValueError(msg)

    @property
    def name(self) -> str:
        if self._operation_def.name:
            return self._operation_def.name.value
        return f"query{capitalize_first(self.stmt.hash_str)}"

    @property
    def variables(self) -> list["GQLVar"]:
        return [
            _parse_var(var_def, schema=self.schema, context=self.stmt.location)
            for var_def in self._operation_def.variable_definitions
        ]

    @property
    def root_type(self) -> graphql.GraphQLObjectType:
        root_type = self.schema.get_root_type(self._operation_def.operation)

        if not root_type:
            msg = f"{self._operation_def.operation} is not defined in the schema"
            raise ValueError(msg)
        return root_type

    @property
    def selection_set(self) -> list["GQLVar"]:
        return _parse_selection_set(
            self._operation_def.selection_set,
            self.root_type,
            schema=self.schema,
            context=self.stmt.location,
        )


@dataclass(kw_only=True)
class GQLVar:
    name: str
    type: "GQLType"
    parent_type: graphql.GraphQLNamedType | None


@dataclass(kw_only=True)
class GQLType:
    not_null: bool
    default_value: graphql.UndefinedType | Any = graphql.Undefined


@dataclass(kw_only=True)
class GQLSingularType(GQLType):
    type: graphql.GraphQLNamedType


@dataclass(kw_only=True)
class GQLObjectType(GQLType):
    type: graphql.GraphQLNamedType
    selection: list[GQLVar]


@dataclass(kw_only=True)
class GQLListType(GQLType):
    type: GQLType


def _error_with_context(message: str, context: str = "") -> str:
    return f"{message} in {context}" if context else message


def _parse_type_node(
    type_node: graphql.TypeNode,
    *,
    schema: graphql.GraphQLSchema,
    not_null: bool = False,
    context: str = "",
) -> GQLType:
    match type_node:
        case graphql.NamedTypeNode(name=name):
            gql_type = schema.get_type(name.value)
            if not gql_type:
                raise ValueError(
                    _error_with_context(f"Unknown type: {name.value}", context)
                )
            return GQLSingularType(type=gql_type, not_null=not_null)
        case graphql.ListTypeNode(type=inner_type):
            inner_gql_type = _parse_type_node(
                inner_type,
                schema=schema,
                not_null=False,
                context=context,
            )
            return GQLListType(type=inner_gql_type, not_null=not_null)
        case graphql.NonNullTypeNode(type=inner_type):
            return _parse_type_node(
                inner_type,
                schema=schema,
                not_null=True,
                context=context,
            )
        case _:
            msg = f"Unsupported type node: {type_node}"
            raise ValueError(msg)


def _parse_selection_set(
    selection_set: graphql.SelectionSetNode,
    parent_type: graphql.GraphQLCompositeType,
    *,
    schema: graphql.GraphQLSchema,
    context: str = "",
) -> list["GQLVar"]:
    return [
        v
        for sel in selection_set.selections
        for v in _parse_selection(sel, parent_type, schema=schema, context=context)
    ]


def _parse_output_type(
    out_type: graphql.GraphQLOutputType,
    *,
    schema: graphql.GraphQLSchema,
    not_null: bool = False,
    selection_set: graphql.SelectionSetNode | None = None,
    context: str = "",
) -> GQLType:
    match out_type:
        case (
            graphql.GraphQLObjectType()
            | graphql.GraphQLInterfaceType()
            | graphql.GraphQLUnionType()
        ):
            if not selection_set:
                type_kind = (
                    "union"
                    if isinstance(out_type, graphql.GraphQLUnionType)
                    else "object"
                )
                msg = f"Selection set is required for {type_kind} types"
                raise ValueError(msg)
            return GQLObjectType(
                type=out_type,
                selection=_parse_selection_set(
                    selection_set, out_type, schema=schema, context=context
                ),
                not_null=not_null,
            )
        case graphql.GraphQLNamedType():
            return GQLSingularType(type=out_type, not_null=not_null)
        case graphql.GraphQLNonNull():
            return _parse_output_type(
                out_type.of_type,
                not_null=True,
                selection_set=selection_set,
                schema=schema,
                context=context,
            )
        case graphql.GraphQLList():
            inner_type = _parse_output_type(
                out_type.of_type,
                not_null=False,
                selection_set=selection_set,
                schema=schema,
                context=context,
            )
            return GQLListType(type=inner_type, not_null=not_null)
        case _:
            msg = f"Unsupported output type: {out_type}"
            raise ValueError(msg)


def _parse_var(
    var_def: graphql.VariableDefinitionNode,
    *,
    schema: graphql.GraphQLSchema,
    context: str = "",
):
    var_name = var_def.variable.name.value
    var_context = _error_with_context(f"variable ${var_name}", context)
    gql_type = _parse_type_node(var_def.type, schema=schema, context=var_context)
    if var_def.default_value is not None:
        gql_type.default_value = value_from_ast_untyped(var_def.default_value)
    return GQLVar(
        name=var_name,
        type=gql_type,
        parent_type=None,
    )


def _parse_selection(
    selection: graphql.SelectionNode,
    parent_type: graphql.GraphQLCompositeType,
    *,
    schema: graphql.GraphQLSchema,
    context: str = "",
) -> list[GQLVar]:
    match selection:
        case graphql.FieldNode(name=name, alias=alias, selection_set=selection_set):
            field = schema_get_field(schema, parent_type, name.value)
            if not field:
                msg = f"Field '{name.value}' not found in type '{parent_type.name}'"
                raise ValueError(_error_with_context(msg, context))

            v = GQLVar(
                name=alias.value if alias else name.value,
                type=_parse_output_type(
                    field.type,
                    selection_set=selection_set,
                    schema=schema,
                    context=context,
                ),
                parent_type=parent_type,
            )
            return [v]
        case graphql.InlineFragmentNode(
            type_condition=tc, selection_set=selection_set
        ) if cast(graphql.NamedTypeNode | None, tc) is None:
            return _parse_selection_set(
                selection_set, parent_type, schema=schema, context=context
            )
        case graphql.InlineFragmentNode(
            type_condition=graphql.NamedTypeNode(
                name=graphql.NameNode(value=fragment_type_name)
            ),
            selection_set=selection_set,
        ):
            fragment_type = resolve_fragment_type(
                schema.get_type(fragment_type_name),
                fragment_type_name,
                parent_type,
            )
            return _parse_selection_set(
                selection_set, fragment_type, schema=schema, context=context
            )
        case graphql.FragmentSpreadNode(name=name):
            msg = f"Fragment spreads not supported: ...{name.value}"
            raise NotImplementedError(msg)
        case _:
            msg = f"Unsupported selection {selection} for parent type {parent_type}"
            raise ValueError(msg)


def resolve_fragment_type(
    fragment_type: graphql.GraphQLNamedType | None,
    fragment_type_name: str,
    parent_type: graphql.GraphQLCompositeType,
) -> graphql.GraphQLCompositeType:
    if not isinstance(
        fragment_type,
        (graphql.GraphQLObjectType, graphql.GraphQLInterfaceType),
    ):
        msg = f"Type condition '{fragment_type_name}' is not a composite type"
        raise TypeError(msg)
    if not isinstance(parent_type, graphql.GraphQLInterfaceType):
        return fragment_type
    if not implements_interface(fragment_type, parent_type):
        msg = (
            f"Type condition '{fragment_type.name}' does not "
            f"implement interface '{parent_type.name}'"
        )
        raise ValueError(msg)
    return fragment_type


def parse_input_type(
    input_type: graphql.GraphQLInputType,
    *,
    not_null: bool = False,
    default_value: Any = graphql.Undefined,
) -> GQLType:
    match input_type:
        case graphql.GraphQLNamedType():
            return GQLSingularType(
                type=input_type,
                not_null=not_null,
                default_value=default_value,
            )
        case graphql.GraphQLNonNull():
            return parse_input_type(
                input_type.of_type,
                not_null=True,
                default_value=default_value,
            )
        case graphql.GraphQLList():
            inner_type = parse_input_type(
                input_type.of_type,
                not_null=False,
                default_value=graphql.Undefined,
            )
            return GQLListType(
                type=inner_type,
                not_null=not_null,
                default_value=default_value,
            )
        case _:
            msg = f"Unsupported input type: {input_type}"
            raise ValueError(msg)


@dataclass(kw_only=True)
class ParseResult:
    queries: list[Query]
    error: str | None


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

    queries = [
        Query(stmt=s, doc=graphql.parse(s.clean_text), schema=schema)
        for s in statements
    ]

    errors = []
    for q in queries:
        errs = graphql.validate(q.schema, q.doc)
        if errs:
            errors.append(
                f"Invalid GraphQL query in {q.stmt.location}:\n"
                + "\n".join(str(e) for e in errs)
            )
            continue
        try:
            _ = q.selection_set
        except (ValueError, TypeError) as exc:
            errors.append(f"Invalid GraphQL query in {q.stmt.location}:\n{exc}")

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
                    "selection_set": q.selection_set,
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


# Port of GraphQLSchema.get_field from graphql-core 3.3
# See https://github.com/graphql-python/graphql-core/blob/main/src/graphql/type/schema.py#L374
def schema_get_field(
    schema: graphql.GraphQLSchema,
    parent_type: graphql.GraphQLCompositeType,
    field_name: str,
) -> graphql.GraphQLField | None:
    if field_name == "__schema":
        return graphql.SchemaMetaFieldDef if schema.query_type is parent_type else None
    if field_name == "__type":
        return graphql.TypeMetaFieldDef if schema.query_type is parent_type else None
    if field_name == "__typename":
        return graphql.TypeNameMetaFieldDef

    try:
        # This is a port not reimplementation, so we use author's approach.
        return parent_type.fields[field_name]  # pyright: ignore[reportAttributeAccessIssue]
    except (AttributeError, KeyError):
        return None


def get_transitive_interfaces(
    type_: graphql.GraphQLObjectType | graphql.GraphQLInterfaceType,
) -> set[graphql.GraphQLInterfaceType]:
    interfaces = set()
    queue = list(type_.interfaces)
    while queue:
        current = queue.pop()
        if current in interfaces:
            continue
        interfaces.add(current)
        queue.extend(current.interfaces)
    return interfaces


def implements_interface(
    type_: graphql.GraphQLObjectType | graphql.GraphQLInterfaceType,
    interface: graphql.GraphQLInterfaceType,
) -> bool:
    return type_ == interface or interface in get_transitive_interfaces(type_)
