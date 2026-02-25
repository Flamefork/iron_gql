import ast
import warnings
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import cast

import graphql
from graphql.execution.collect_fields import collect_fields
from graphql.execution.execute import get_field_def
from pydantic import alias_generators

from iron_gql.codegen.parser import GQLVar
from iron_gql.codegen.parser import Query
from iron_gql.codegen.parser import Statement
from iron_gql.codegen.parser import parse_gql_queries
from iron_gql.codegen.util import capitalize_first
from iron_gql.codegen.util import indent_block
from iron_gql.codegen.util import write_if_changed

type StrTransform = Callable[[str], str]


class GraphQLGenerationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


BUILTIN_SCALARS = {
    "String": "str",
    "Int": "int",
    "Float": "float",
    "Boolean": "bool",
    "Date": "datetime.date",
    "DateTime": "datetime.datetime",
    "JSON": "object",
    "Upload": "runtime.FileVar",
}


class UnknownGQLTypeWarning(UserWarning):
    pass


@dataclass(kw_only=True, frozen=True)
class GeneratedModel:
    name: str
    fields: list[str]


@dataclass(kw_only=True, frozen=True)
class GeneratedTypeAlias:
    name: str
    type_expr: str


type GeneratedArtifact = GeneratedModel | GeneratedTypeAlias


@dataclass(kw_only=True, frozen=True)
class RenderContext:
    fragments: dict[str, graphql.FragmentDefinitionNode]
    variable_values: dict[str, Any]


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


def collect_type_conditions(
    selection_set: graphql.SelectionSetNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
) -> set[str]:
    visited_fragments: set[str] = set()
    conditions: set[str] = set()

    def walk(selection_set: graphql.SelectionSetNode) -> None:
        for selection in selection_set.selections:
            if isinstance(selection, graphql.FieldNode):
                continue
            if isinstance(selection, graphql.InlineFragmentNode):
                type_condition = cast(
                    graphql.NamedTypeNode | None, selection.type_condition
                )
                if type_condition is not None:
                    conditions.add(type_condition.name.value)
                walk(selection.selection_set)
                continue
            if isinstance(selection, graphql.FragmentSpreadNode):
                name = selection.name.value
                if name in visited_fragments:
                    continue
                visited_fragments.add(name)
                fragment = fragments.get(name)
                if fragment is None:
                    continue
                conditions.add(fragment.type_condition.name.value)
                walk(fragment.selection_set)
                continue
            msg = f"Unsupported selection {selection}"
            raise TypeError(msg)

    walk(selection_set)
    return conditions


def interface_has_base_typename(
    selection_set: graphql.SelectionSetNode,
    fragments: dict[str, graphql.FragmentDefinitionNode],
    interface_name: str,
) -> bool:
    visited_fragments: set[str] = set()
    stack: list[graphql.SelectionSetNode] = [selection_set]
    while stack:
        current = stack.pop()
        for selection in current.selections:
            if isinstance(selection, graphql.FieldNode):
                if selection.alias is None and selection.name.value == "__typename":
                    return True
                continue
            if isinstance(selection, graphql.InlineFragmentNode):
                type_cond = cast(graphql.NamedTypeNode | None, selection.type_condition)
                if type_cond is None or type_cond.name.value == interface_name:
                    stack.append(selection.selection_set)
                continue
            if isinstance(selection, graphql.FragmentSpreadNode):
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
                continue
            msg = f"Unsupported selection {selection}"
            raise TypeError(msg)
    return False


def build_codegen_variable_values(
    doc: graphql.DocumentNode,
    variables: Iterable[GQLVar],
) -> dict[str, Any]:
    values: dict[str, Any] = {
        var.name: var.default_value
        for var in variables
        if var.default_value != graphql.Undefined
    }
    desired: dict[str, bool] = {}

    class DirectiveVarCollector(graphql.Visitor):
        def enter_directive(self, node: graphql.DirectiveNode, *_args: object) -> None:
            directive_name = node.name.value
            if directive_name == "include":
                include_value = True
            elif directive_name == "skip":
                include_value = False
            else:
                return

            for arg in node.arguments or []:
                if arg.name.value != "if":
                    continue
                if isinstance(arg.value, graphql.VariableNode):
                    var_name = arg.value.name.value
                    prev = desired.get(var_name)
                    if prev is None:
                        desired[var_name] = include_value
                    else:
                        desired[var_name] = prev or include_value

    graphql.visit(doc, DirectiveVarCollector())
    for name, value in desired.items():
        if name not in values:
            values[name] = value
    return values


def render_type(
    gql_type: graphql.GraphQLType,
    scalars: dict[str, str],
    *,
    nullable: bool = True,
    child_model_name: str | None = None,
    enums: set[graphql.GraphQLEnumType] | None = None,
) -> str:
    match gql_type:
        case graphql.GraphQLNonNull(of_type=inner):
            return render_type(
                inner,
                scalars,
                nullable=False,
                child_model_name=child_model_name,
                enums=enums,
            )
        case graphql.GraphQLList(of_type=inner):
            inner_str = render_type(
                inner, scalars, child_model_name=child_model_name, enums=enums
            )
            typ = f"list[{inner_str}]"
        case graphql.GraphQLNamedType() if child_model_name is not None:
            typ = child_model_name
        case graphql.GraphQLNamedType():
            typ = field_py_type(gql_type, scalars, enums=enums)
        case _:
            msg = f"Unknown GraphQL type: {gql_type}"
            raise TypeError(msg)
    if nullable:
        typ += " | None"
    return typ


def render_pydantic_class(name: str, base: str, fields: list[str]) -> str:
    return f"class {name}({base}):\n    {indent_block('\n'.join(fields), '    ')}"


def generate_gql_package(
    *,
    schema_path: Path,
    package_full_name: str,
    base_url_import: str,
    scalars: dict[str, str] | None = None,
    to_camel_fn_full_name: str = "pydantic.alias_generators:to_camel",
    to_snake_fn: StrTransform = alias_generators.to_snake,
    debug_path: Path | None = None,
    src_path: Path,
) -> bool:
    """Generate a typed GraphQL client from schema and discovered queries.

    Scans src_path for calls to `<package>_gql()`, validates queries against
    schema_path, and generates a module with Pydantic models and typed query
    classes with async execution methods.

    Args:
        schema_path: Path to GraphQL SDL schema file
        package_full_name: Full module name for generated package
            (e.g., "myapp.gql.client")
        base_url_import: Import path to base URL
            (e.g., "myapp.config:GRAPHQL_URL")
        scalars: Custom GraphQL scalar to Python type mapping
            (e.g., {"ID": "builtins:str"})
        to_camel_fn_full_name: Import path to camelCase conversion function
        to_snake_fn: Function for converting names to snake_case
        debug_path: Optional path for saving debug artifacts
        src_path: Root directory to search for GraphQL query calls

    Returns:
        True if the generated file was modified, False if content unchanged

    Raises:
        GraphQLGenerationError: If any query fails schema validation
    """
    if scalars is None:
        scalars = {}

    package_name = package_full_name.rsplit(".", maxsplit=1)[-1]
    gql_fn_name = f"{package_name}_gql"

    target_package_path = src_path / f"{package_full_name.replace('.', '/')}.py"
    base_url_import_package, base_url_import_path = base_url_import.split(":")

    queries = list(
        find_all_queries(src_path, gql_fn_name, skip_path=target_package_path)
    )

    parse_res = parse_gql_queries(
        schema_path,
        queries,
        debug_path=debug_path,
    )

    if parse_res.errors:
        raise GraphQLGenerationError(parse_res.errors)

    new_content = render_package(
        base_url_import_package=base_url_import_package,
        base_url_import_path=base_url_import_path,
        package_name=package_name,
        gql_fn_name=gql_fn_name,
        queries=sorted(parse_res.queries, key=lambda q: q.name),
        scalars=scalars,
        to_camel_fn_full_name=to_camel_fn_full_name,
        to_snake_fn=to_snake_fn,
    )
    return write_if_changed(target_package_path, new_content + "\n")


def find_fn_calls(
    root_path: Path, fn_name: str, *, skip_path: Path
) -> Iterator[tuple[Path, int, ast.Call]]:
    for path in root_path.glob("**/*.py"):
        if path.resolve() == skip_path.resolve():
            continue
        content = path.read_text(encoding="utf-8")
        if fn_name not in content:
            continue
        for node in ast.walk(ast.parse(content, filename=str(path))):
            match node:
                case ast.Call(func=ast.Name(id=id)) if id == fn_name:
                    yield path, node.lineno, node
                case _:
                    pass


def find_all_queries(
    src_path: Path, gql_fn_name: str, *, skip_path: Path
) -> Iterator[Statement]:
    for file, lineno, node in find_fn_calls(src_path, gql_fn_name, skip_path=skip_path):
        relative_path = file.relative_to(src_path)

        stmt_arg = node.args[0]
        if (
            len(node.args) != 1
            or not isinstance(stmt_arg, ast.Constant)
            or not isinstance(stmt_arg.value, str)
        ):
            msg = (
                f"Invalid positional arguments for {gql_fn_name} "
                f"at {relative_path}:{lineno}, "
                "expected a single string literal"
            )
            raise TypeError(msg)

        yield Statement(raw_text=stmt_arg.value, file=relative_path, lineno=lineno)


HEADER = """\
from __future__ import annotations
# Code generated by iron_gql, DO NOT EDIT.
# fmt: off
# pyright: reportUnusedImport=false
# ruff: noqa"""


def render_imports(
    to_camel_fn_full_name: str,
    scalars: dict[str, str],
    base_url_import_package: str,
    base_url_import_path: str,
) -> str:
    import_modules = [m.split(":")[0] for m in scalars.values()]
    scalar_imports = "\n".join(f"import {m}" for m in import_modules)
    to_camel_module = to_camel_fn_full_name.split(":", maxsplit=1)[0]
    base_url_symbol = base_url_import_path.split(".", maxsplit=1)[0]
    return f"""\
import datetime
from typing import Annotated
from typing import Literal
from typing import overload

import pydantic

from iron_gql import runtime

import {to_camel_module}

{scalar_imports}

from {base_url_import_package} import {base_url_symbol}"""


def render_client_init(
    package_name: str,
    base_url_import_path: str,
    to_camel_fn_full_name: str,
) -> str:
    return f"""\
{package_name.upper()}_CLIENT = runtime.GQLClient(
    base_url={base_url_import_path},
)


class GQLModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        populate_by_name=True,
        alias_generator={to_camel_fn_full_name.replace(":", ".")},
        extra="forbid",
        validate_default=True,
    )"""


def render_gql_fn(
    queries: list[Query],
    package_name: str,
    gql_fn_name: str,
) -> str:
    dict_name = f"_{package_name.upper()}_GQL_DISPATCH"

    entries = [(repr(q.stmt.raw_text), capitalize_first(q.name)) for q in queries]

    overloads = "\n".join(
        f"@overload\ndef {gql_fn_name}(stmt: Literal[{lit}]) -> {ret}: ..."
        for lit, ret in entries
    )
    dict_entries = "\n".join(f"    {lit}: {ret}," for lit, ret in entries)

    return f"""\
{overloads}
@overload
def {gql_fn_name}(stmt: str) -> runtime.GQLQuery: ...


{dict_name}: dict[str, type[runtime.GQLQuery]] = {{
{dict_entries}
}}


def {gql_fn_name}(stmt: str) -> runtime.GQLQuery:
    query_cls = {dict_name}.get(stmt)
    if query_cls is not None:
        return query_cls()
    return runtime.GQLQuery()"""


def render_package(
    base_url_import_package: str,
    base_url_import_path: str,
    package_name: str,
    gql_fn_name: str,
    queries: list[Query],
    scalars: dict[str, str],
    to_camel_fn_full_name: str,
    to_snake_fn: StrTransform,
) -> str:
    queries = get_unique_queries(queries)

    input_roots: set[graphql.GraphQLInputObjectType] = set()
    for query in queries:
        for v in query.variables:
            collect_input_root(v.gql_type, input_roots)
    ordered_input_types = collect_input_types(input_roots)

    if queries:
        renderer = PackageRenderer(
            schema=queries[0].schema, scalars=scalars, to_snake_fn=to_snake_fn
        )
        rendered_result_models = renderer.render_result_models(queries)
        rendered_input_types = renderer.render_input_types(ordered_input_types)
        rendered_query_classes = renderer.render_query_classes(queries, package_name)
        rendered_enums = renderer.render_enums()
    else:
        rendered_result_models = []
        rendered_input_types = []
        rendered_query_classes = []
        rendered_enums = []

    sections = [
        HEADER,
        render_imports(
            to_camel_fn_full_name,
            scalars,
            base_url_import_package,
            base_url_import_path,
        ),
        render_client_init(package_name, base_url_import_path, to_camel_fn_full_name),
        "\n".join(rendered_enums),
        "\n\n\n".join(rendered_result_models),
        "\n\n\n".join(rendered_input_types),
        "\n\n\n".join(rendered_query_classes),
        render_gql_fn(queries, package_name, gql_fn_name),
    ]
    return "\n\n\n".join(section for section in sections if section)


def collect_input_root(
    gql_type: graphql.GraphQLType,
    roots: set[graphql.GraphQLInputObjectType],
) -> None:
    named = graphql.get_named_type(gql_type)
    if isinstance(named, graphql.GraphQLInputObjectType):
        roots.add(named)


def get_unique_queries(queries: list[Query]) -> list[Query]:
    unique_queries: dict[str, Query] = {}
    for query in queries:
        if query.name not in unique_queries:
            unique_queries[query.name] = query
        elif unique_queries[query.name].stmt.hash_str != query.stmt.hash_str:
            msg = (
                f"Cannot compile different GraphQL queries with same name {query.name}"
                f" at {query.stmt.location}"
                f" and {unique_queries[query.name].stmt.location}"
            )
            raise ValueError(msg)
    return list(unique_queries.values())


@dataclass(kw_only=True)
class PackageRenderer:
    scalars: dict[str, str]
    to_snake_fn: StrTransform
    schema: graphql.GraphQLSchema
    enums: set[graphql.GraphQLEnumType] = field(default_factory=set)

    def render_operation_models(self, query: Query) -> list[GeneratedArtifact]:
        ctx = RenderContext(
            fragments=query.fragments,
            variable_values=build_codegen_variable_values(query.doc, query.variables),
        )
        return self._render_object_model(
            model_name_base=f"{capitalize_first(query.name)}Result",
            runtime_type=query.root_type,
            selection_set=query.operation_def.selection_set,
            ctx=ctx,
        )

    def _render_object_model(
        self,
        *,
        model_name_base: str,
        runtime_type: graphql.GraphQLObjectType,
        selection_set: graphql.SelectionSetNode,
        ctx: RenderContext,
        typename_type: str | None = None,
        require_typename_for: str | None = None,
    ) -> list[GeneratedArtifact]:
        fields_by_key = collect_fields(
            self.schema,
            ctx.fragments,
            ctx.variable_values,
            runtime_type,
            selection_set,
        )
        if require_typename_for is not None and "__typename" not in fields_by_key:
            msg = f"Missing __typename in selection set for '{require_typename_for}'"
            raise ValueError(msg)

        child_models: list[GeneratedArtifact] = []
        fields_mapping: dict[str, str] = {}
        for response_key, field_nodes in fields_by_key.items():
            representative = field_nodes[0]
            if response_key.startswith("__"):
                py_name = self.to_snake_fn(f"{response_key[2:]}__")
                py_alias = response_key
            else:
                py_name = self.to_snake_fn(response_key)
                py_alias = None

            if response_key == "__typename":
                field_type = typename_type or f'Literal["{runtime_type.name}"]'
            else:
                field_def = cast(
                    graphql.GraphQLField | None,
                    get_field_def(self.schema, runtime_type, representative),
                )
                if field_def is None:
                    msg = (
                        f"Field '{representative.name.value}' not found in type "
                        f"'{runtime_type.name}'"
                    )
                    raise ValueError(msg)
                field_child_models, field_type = self._render_typed_field(
                    model_name_base=model_name_base,
                    response_key=response_key,
                    gql_type=field_def.type,
                    field_nodes=field_nodes,
                    ctx=ctx,
                )
                child_models.extend(field_child_models)

            if py_alias:
                field_type = f'{field_type} = pydantic.Field(alias="{py_alias}")'
            fields_mapping[py_name] = field_type

        return [
            *child_models,
            GeneratedModel(
                name=model_name_base,
                fields=[
                    f"{field}: {field_type}"
                    for field, field_type in fields_mapping.items()
                ],
            ),
        ]

    def _render_typed_field(
        self,
        *,
        model_name_base: str,
        response_key: str,
        gql_type: graphql.GraphQLType,
        field_nodes: list[graphql.FieldNode],
        ctx: RenderContext,
    ) -> tuple[list[GeneratedArtifact], str]:
        selection_set = merge_selection_sets(field_nodes)
        if selection_set is None:
            return [], render_type(gql_type, self.scalars, enums=self.enums)

        named = graphql.get_named_type(gql_type)
        child_base = model_name_base + capitalize_first(response_key)
        if isinstance(named, graphql.GraphQLObjectType):
            return (
                self._render_object_model(
                    model_name_base=child_base,
                    runtime_type=named,
                    selection_set=selection_set,
                    ctx=ctx,
                ),
                render_type(
                    gql_type,
                    self.scalars,
                    child_model_name=child_base,
                    enums=self.enums,
                ),
            )

        if isinstance(named, graphql.GraphQLUnionType):
            possible = sorted(named.types, key=lambda t: t.name)
            return self._render_polymorphic_models(
                base_name=child_base,
                possible_types=possible,
                explicit_types=set(possible),
                selection_set=selection_set,
                ctx=ctx,
                field_gql_type=gql_type,
                require_typename_for=named.name,
            )

        if isinstance(named, graphql.GraphQLInterfaceType):
            possible = sorted(
                self.schema.get_possible_types(named),
                key=lambda t: t.name,
            )
            if not possible:
                msg = f"Interface '{named.name}' has no possible types"
                raise ValueError(msg)
            explicit = self._resolve_explicit_types(selection_set, ctx, named, possible)
            if explicit and not interface_has_base_typename(
                selection_set, ctx.fragments, named.name
            ):
                msg = (
                    f"Missing __typename in selection set for interface '{named.name}'"
                )
                raise ValueError(msg)
            return self._render_polymorphic_models(
                base_name=child_base,
                possible_types=possible,
                explicit_types=explicit,
                selection_set=selection_set,
                ctx=ctx,
                field_gql_type=gql_type,
                require_typename_for=named.name,
            )

        msg = f"Unknown type {named} for field {response_key}"
        raise ValueError(msg)

    def _resolve_explicit_types(
        self,
        selection_set: graphql.SelectionSetNode,
        ctx: RenderContext,
        interface_type: graphql.GraphQLInterfaceType,
        possible_types: list[graphql.GraphQLObjectType],
    ) -> set[graphql.GraphQLObjectType]:
        explicit_conditions = collect_type_conditions(selection_set, ctx.fragments) - {
            interface_type.name
        }
        explicit_objects: set[graphql.GraphQLObjectType] = set()
        for name in explicit_conditions:
            typ = self.schema.get_type(name)
            if typ is None:
                msg = f"Unknown GraphQL type '{name}'"
                raise ValueError(msg)
            if isinstance(typ, graphql.GraphQLObjectType):
                explicit_objects.add(typ)
                continue
            if isinstance(typ, graphql.GraphQLInterfaceType):
                possible_set = set(possible_types)
                explicit_objects.update(
                    set(self.schema.get_possible_types(typ)) & possible_set
                )
                continue
            msg = f"Type condition '{name}' is not a composite type"
            raise TypeError(msg)
        return explicit_objects

    def _render_polymorphic_models(
        self,
        *,
        base_name: str,
        possible_types: list[graphql.GraphQLObjectType],
        explicit_types: set[graphql.GraphQLObjectType],
        selection_set: graphql.SelectionSetNode,
        ctx: RenderContext,
        field_gql_type: graphql.GraphQLType,
        require_typename_for: str,
    ) -> tuple[list[GeneratedArtifact], str]:
        if not explicit_types:
            child_models = self._render_object_model(
                model_name_base=base_name,
                runtime_type=possible_types[0],
                selection_set=selection_set,
                ctx=ctx,
                typename_type="str",
            )
            return (
                child_models,
                render_type(
                    field_gql_type,
                    self.scalars,
                    child_model_name=base_name,
                    enums=self.enums,
                ),
            )

        child_models: list[GeneratedArtifact] = []
        union_types: list[str] = []
        for obj_type in sorted(explicit_types, key=lambda t: t.name):
            model_name = base_name + obj_type.name
            child_models.extend(
                self._render_object_model(
                    model_name_base=model_name,
                    runtime_type=obj_type,
                    selection_set=selection_set,
                    ctx=ctx,
                    require_typename_for=require_typename_for,
                )
            )
            union_types.append(model_name)

        fallback_objects = {t for t in possible_types if t not in explicit_types}
        if fallback_objects:
            fallback_name = base_name + require_typename_for
            fallback_type_names = sorted(obj.name for obj in fallback_objects)
            fallback_typename_literal = (
                f"Literal[{', '.join(repr(name) for name in fallback_type_names)}]"
            )
            fallback_runtime = min(fallback_objects, key=lambda t: t.name)
            child_models.extend(
                self._render_object_model(
                    model_name_base=fallback_name,
                    runtime_type=fallback_runtime,
                    selection_set=selection_set,
                    ctx=ctx,
                    typename_type=fallback_typename_literal,
                    require_typename_for=require_typename_for,
                )
            )
            union_types.append(fallback_name)

        union_str = " | ".join(union_types)
        alias_value = (
            f'Annotated[{union_str}, pydantic.Field(discriminator="typename__")]'
        )
        return (
            [*child_models, GeneratedTypeAlias(name=base_name, type_expr=alias_value)],
            render_type(
                field_gql_type,
                self.scalars,
                child_model_name=base_name,
                enums=self.enums,
            ),
        )

    def render_result_models(self, queries: list[Query]) -> list[str]:
        rendered: list[str] = []
        for query in queries:
            for item in self.render_operation_models(query):
                match item:
                    case GeneratedModel():
                        rendered.append(
                            render_pydantic_class(item.name, "GQLModel", item.fields)
                        )
                    case GeneratedTypeAlias():
                        rendered.append(f"type {item.name} = {item.type_expr}")
        return rendered

    def render_query_classes(
        self, queries: list[Query], package_name: str
    ) -> list[str]:
        query_classes = []
        for query in queries:
            args = ["self"]
            variables = []
            if query.variables:
                args.append("*")
            for v in query.variables:
                py_name = self.to_snake_fn(v.name)
                typ = render_type(v.gql_type, self.scalars, enums=self.enums)
                if v.default_value != graphql.Undefined:
                    typ += f" = {v.default_value!r}"
                args.append(f"{py_name}: {typ}")
                variables.append(f'"{v.name}": runtime.serialize_var({py_name})')
            variables_arg = (
                f"\n            {{{', '.join(variables)}}}," if variables else ""
            )

            query_classes.append(
                f"""

class {capitalize_first(query.name)}(runtime.GQLQuery):
    async def execute({", ".join(args)}) -> {capitalize_first(query.name)}Result:
        return await {package_name.upper()}_CLIENT.query(
            {capitalize_first(query.name)}Result,
            {query.exec_source!r},{variables_arg}
            headers=self.headers,
            upload_files=self.upload_files,
        )

                """.strip()
            )
        return query_classes

    def render_input_types(
        self, ordered_input_types: list[graphql.GraphQLInputObjectType]
    ) -> list[str]:
        rendered: list[str] = []
        for typ in ordered_input_types:
            fields: list[str] = []
            for field_name, gql_field in typ.fields.items():
                typ_str = render_type(gql_field.type, self.scalars, enums=self.enums)
                if gql_field.default_value != graphql.Undefined:
                    typ_str += f" = {gql_field.default_value!r}"
                elif not isinstance(gql_field.type, graphql.GraphQLNonNull):
                    typ_str += " = None"
                fields.append(f"{self.to_snake_fn(field_name)}: {typ_str}")
            rendered.append(render_pydantic_class(typ.name, "GQLModel", fields))
        return rendered

    def render_enums(self) -> list[str]:
        return [
            f"type {typ.name} = Literal[{', '.join(repr(name) for name in typ.values)}]"
            for typ in sorted(self.enums, key=lambda t: t.name)
        ]


def collect_input_types(
    roots: Iterable[graphql.GraphQLInputObjectType],
) -> list[graphql.GraphQLInputObjectType]:
    visited: set[str] = set()
    result: list[graphql.GraphQLInputObjectType] = []
    queue = sorted(roots, key=lambda t: t.name)
    while queue:
        typ = queue.pop()
        if typ.name in visited:
            continue
        visited.add(typ.name)
        result.append(typ)
        for gql_field in typ.fields.values():
            target = graphql.get_named_type(gql_field.type)
            if isinstance(target, graphql.GraphQLInputObjectType):
                queue.append(target)
    return sorted(result, key=lambda t: t.name)


def field_py_type(
    gql_type: graphql.GraphQLNamedType,
    scalars: dict[str, str],
    *,
    enums: set[graphql.GraphQLEnumType] | None = None,
) -> str:
    match gql_type:
        case graphql.GraphQLScalarType(name=name):
            if name in scalars:
                return scalars[name].replace(":", ".")
            if name in BUILTIN_SCALARS:
                return BUILTIN_SCALARS[name]
            warnings.warn(
                f"Unknown scalar type: {name}, mapped to 'object'",
                category=UnknownGQLTypeWarning,
                stacklevel=1,
            )
            return "object"
        case graphql.GraphQLInputObjectType(name=name):
            return name
        case graphql.GraphQLEnumType(name=name):
            if enums is not None:
                enums.add(gql_type)
            return name
        case _:
            warnings.warn(
                f"Unknown GraphQL type: {gql_type.name}"
                f" ({type(gql_type).__name__}), mapped to 'object'",
                category=UnknownGQLTypeWarning,
                stacklevel=1,
            )
            return "object"
