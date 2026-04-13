import ast
import hashlib
import re
import warnings
from collections import defaultdict
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


class GraphQLDeprecationWarning(UserWarning):
    pass


@dataclass(kw_only=True, frozen=True)
class GeneratedModel:
    name: str
    fields: list[str]
    graphql_type_name: str | None = None


@dataclass(kw_only=True, frozen=True)
class GeneratedTypeAlias:
    name: str
    type_expr: str


type GeneratedArtifact = GeneratedModel | GeneratedTypeAlias


@dataclass(kw_only=True, frozen=True)
class RenderContext:
    query_name: str
    fragments: dict[str, graphql.FragmentDefinitionNode]
    variable_values: dict[str, Any]
    excluded_variable_values: dict[str, Any]


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
    directive_targets = {"include": include_value, "skip": skip_value}
    # When building "include" values (include_value=True), max keeps True if any
    # directive wants the field included. When building "exclude" values
    # (include_value=False), min keeps False if any directive wants it excluded.
    merge = max if include_value else min

    class DirectiveVarCollector(graphql.Visitor):
        def enter_directive(self, node: graphql.DirectiveNode, *_args: object) -> None:
            target = directive_targets.get(node.name.value)
            if target is None:
                return

            for arg in node.arguments or []:
                if arg.name.value != "if":
                    continue
                if isinstance(arg.value, graphql.VariableNode):
                    var_name = arg.value.name.value
                    prev = desired.get(var_name)
                    desired[var_name] = target if prev is None else merge(prev, target)

    graphql.visit(doc, DirectiveVarCollector())
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


def _extract_field_name(field_def: str) -> str:
    return field_def.split(":", maxsplit=1)[0].strip()


def _names_pattern(names: Iterable[str]) -> re.Pattern[str]:
    return re.compile(
        r"\b("
        + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        + r")\b"
    )


def _apply_renames(text: str, rename: dict[str, str]) -> str:
    if not rename:
        return text
    return _names_pattern(rename).sub(lambda m: rename[m.group()], text)


def _build_dependency_graph(
    models: list[GeneratedModel],
) -> tuple[dict[str, GeneratedModel], dict[str, set[str]]]:
    model_names = {m.name for m in models}
    name_pattern = _names_pattern(model_names)
    deps: dict[str, set[str]] = {}
    by_name: dict[str, GeneratedModel] = {}
    for m in models:
        by_name[m.name] = m
        refs: set[str] = set()
        for f in m.fields:
            refs.update(name_pattern.findall(f))
        refs.discard(m.name)
        deps[m.name] = refs
    return by_name, deps


def _topological_sort(models: list[GeneratedModel]) -> list[GeneratedModel]:
    if not models:
        return models
    by_name, deps = _build_dependency_graph(models)

    in_degree: dict[str, int] = dict.fromkeys(deps, 0)
    for name, refs in deps.items():
        for ref in refs:
            if ref in in_degree:
                in_degree[name] += 1

    queue = sorted(name for name, deg in in_degree.items() if deg == 0)
    result: list[GeneratedModel] = []
    while queue:
        name = queue.pop(0)
        result.append(by_name[name])
        for other, refs in deps.items():
            if name in refs:
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    queue.append(other)
                    queue.sort()

    return result


def _field_name_to_pascal(name: str) -> str:
    return "".join(
        capitalize_first(part) for part in name.strip("_").split("_") if part
    )


def _fields_hash(canonical: tuple[str, ...]) -> str:
    return hashlib.md5(
        "\n".join(canonical).encode(), usedforsecurity=False
    ).hexdigest()[:6]


type _ShapeFamily = dict[tuple[str, ...], tuple[str, str]]


def _short_model_name(
    graphql_type: str,
    original_name: str,
    fields: tuple[str, ...],
    families: dict[str, _ShapeFamily],
    rename: dict[str, str],
) -> str:
    field_names = sorted(_extract_field_name(f) for f in fields)
    suffix = "".join(_field_name_to_pascal(n) for n in field_names)
    candidate = f"{graphql_type}With{suffix}"
    canonical = tuple(sorted(fields))

    family = families.get(candidate)
    if family is None:
        families[candidate] = {canonical: (candidate, original_name)}
        return candidate

    existing = family.get(canonical)
    if existing is not None:
        return existing[0]

    # Collision: same candidate, different shape.
    # If the family has one entry sitting on the plain candidate, retroactively hash it.
    if len(family) == 1:
        old_canonical, (old_resolved, old_original) = next(iter(family.items()))
        if old_resolved == candidate:
            old_hashed = f"{candidate}_{_fields_hash(old_canonical)}"
            family[old_canonical] = (old_hashed, old_original)
            rename[old_original] = old_hashed

    new_hashed = f"{candidate}_{_fields_hash(canonical)}"
    family[canonical] = (new_hashed, original_name)
    return new_hashed


def _build_rename_map(artifacts: list[GeneratedArtifact]) -> dict[str, str]:
    rename: dict[str, str] = {}
    families: dict[str, _ShapeFamily] = {}

    models = [
        a
        for a in artifacts
        if isinstance(a, GeneratedModel) and a.graphql_type_name is not None
    ]
    models = _topological_sort(models)

    for model in models:
        graphql_type_name = model.graphql_type_name
        if graphql_type_name is None:
            continue
        final_fields = tuple(_apply_renames(f, rename) for f in model.fields)
        short = _short_model_name(
            graphql_type_name, model.name, final_fields, families, rename
        )
        if model.name != short:
            rename[model.name] = short

    return rename


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
        queries=parse_res.queries,
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
        try:
            tree = ast.parse(content, filename=str(path))
        except SyntaxError as exc:
            msg = f"Failed to parse {path}: {exc.msg} (line {exc.lineno})"
            raise SyntaxError(msg) from exc
        for node in ast.walk(tree):
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
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
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
def {gql_fn_name}(stmt: str) -> runtime.GQLOperation: ...


{dict_name}: dict[str, type[runtime.GQLOperation]] = {{
{dict_entries}
}}


def {gql_fn_name}(stmt: str) -> runtime.GQLOperation:
    query_cls = {dict_name}.get(stmt)
    if query_cls is not None:
        return query_cls()
    return runtime.GQLOperation()"""


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
    queries, all_locations = get_unique_queries(queries)

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
        rendered_query_classes = renderer.render_query_classes(
            queries, package_name, all_locations
        )
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


def get_unique_queries(
    queries: list[Query],
) -> tuple[list[Query], dict[str, list[str]]]:
    all_locations: dict[str, list[str]] = defaultdict(list)
    first_occurrence: dict[str, Query] = {}
    for query in queries:
        all_locations[query.name].append(query.stmt.location)
        if query.name not in first_occurrence:
            first_occurrence[query.name] = query
        elif first_occurrence[query.name].stmt.hash_str != query.stmt.hash_str:
            msg = (
                f"Cannot compile different GraphQL queries with same name {query.name}"
                f" at {', '.join(all_locations[query.name])}"
            )
            raise ValueError(msg)
    unique = sorted(
        first_occurrence.values(), key=lambda q: (q.stmt.file, q.stmt.lineno)
    )
    return unique, dict(all_locations)


def _warn_deprecated_input_field(
    type_name: str,
    field_name: str,
    deprecation_reason: str,
) -> None:
    warnings.warn(
        f"Input field '{type_name}.{field_name}' is deprecated: {deprecation_reason}",
        GraphQLDeprecationWarning,
        stacklevel=2,
    )


def _warn_deprecated_field(
    query_name: str,
    runtime_type: graphql.GraphQLObjectType,
    representative: graphql.FieldNode,
    field_def: graphql.GraphQLField,
) -> None:
    if field_def.deprecation_reason is not None:
        warnings.warn(
            f"Query '{query_name}': field"
            f" '{runtime_type.name}.{representative.name.value}'"
            f" is deprecated: {field_def.deprecation_reason}",
            GraphQLDeprecationWarning,
            stacklevel=2,
        )
    if representative.arguments:
        for arg_node in representative.arguments:
            schema_arg = field_def.args[arg_node.name.value]
            if schema_arg.deprecation_reason is not None:
                warnings.warn(
                    f"Query '{query_name}': argument"
                    f" '{arg_node.name.value}'"
                    f" on '{runtime_type.name}.{representative.name.value}'"
                    f" is deprecated:"
                    f" {schema_arg.deprecation_reason}",
                    GraphQLDeprecationWarning,
                    stacklevel=2,
                )


@dataclass(kw_only=True)
class PackageRenderer:
    scalars: dict[str, str]
    to_snake_fn: StrTransform
    schema: graphql.GraphQLSchema
    enums: set[graphql.GraphQLEnumType] = field(default_factory=set)

    def render_operation_models(self, query: Query) -> list[GeneratedArtifact]:
        ctx = RenderContext(
            query_name=query.name,
            fragments=query.fragments,
            variable_values=build_codegen_variable_values(query.doc, query.variables),
            excluded_variable_values=build_excluded_variable_values(
                query.doc, query.variables
            ),
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
        graphql_type_name: str | None = None,
    ) -> list[GeneratedArtifact]:
        codegen_fields = collect_fields(
            self.schema,
            ctx.fragments,
            ctx.variable_values,
            runtime_type,
            selection_set,
        )
        if require_typename_for is not None and "__typename" not in codegen_fields:
            msg = f"Missing __typename in selection set for '{require_typename_for}'"
            raise ValueError(msg)

        excluded_fields = collect_fields(
            self.schema,
            ctx.fragments,
            ctx.excluded_variable_values,
            runtime_type,
            selection_set,
        )
        fields_by_key = {**excluded_fields, **codegen_fields}
        conditional_keys = fields_by_key.keys() - (
            codegen_fields.keys() & excluded_fields.keys()
        )

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
                _warn_deprecated_field(
                    ctx.query_name, runtime_type, representative, field_def
                )
                field_child_models, field_type = self._render_typed_field(
                    model_name_base=model_name_base,
                    response_key=response_key,
                    gql_type=field_def.type,
                    field_nodes=field_nodes,
                    ctx=ctx,
                )
                child_models.extend(field_child_models)

            is_conditional = response_key in conditional_keys
            if is_conditional and not field_type.endswith("| None"):
                field_type += " | None"

            if py_alias and is_conditional:
                field_type = (
                    f'{field_type} = pydantic.Field(alias="{py_alias}", default=None)'
                )
            elif py_alias:
                field_type = f'{field_type} = pydantic.Field(alias="{py_alias}")'
            elif is_conditional:
                field_type += " = None"
            fields_mapping[py_name] = field_type

        return [
            *child_models,
            GeneratedModel(
                name=model_name_base,
                fields=[
                    f"{field}: {field_type}"
                    for field, field_type in fields_mapping.items()
                ],
                graphql_type_name=graphql_type_name,
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
                    graphql_type_name=named.name,
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
                graphql_type_name=require_typename_for,
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
                    graphql_type_name=obj_type.name,
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
                    graphql_type_name=require_typename_for,
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
        all_artifacts: list[GeneratedArtifact] = []
        for query in queries:
            all_artifacts.extend(self.render_operation_models(query))

        rename = _build_rename_map(all_artifacts)

        seen: set[str] = set()
        rendered: list[str] = []
        for item in all_artifacts:
            match item:
                case GeneratedModel():
                    name = rename.get(item.name, item.name)
                    if name in seen:
                        continue
                    seen.add(name)
                    fields = [_apply_renames(f, rename) for f in item.fields]
                    rendered.append(render_pydantic_class(name, "GQLModel", fields))
                case GeneratedTypeAlias():
                    expr = _apply_renames(item.type_expr, rename)
                    rendered.append(f"type {item.name} = {expr}")
        return rendered

    def render_query_classes(
        self,
        queries: list[Query],
        package_name: str,
        all_locations: dict[str, list[str]],
    ) -> list[str]:
        query_classes = []
        for query in queries:
            is_subscription = (
                query.operation_def.operation == graphql.OperationType.SUBSCRIPTION
            )
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
                variables.append(f'"{v.name}": {py_name}')
            variables_str = f"{{{', '.join(variables)}}}"

            class_name = capitalize_first(query.name)
            result_type = f"{class_name}Result"
            see_comment = f"# See: {', '.join(all_locations[query.name])}"

            if is_subscription:
                query_classes.append(
                    f"""

class {class_name}(runtime.GQLOperation):
    {see_comment}
    def execute({", ".join(args)}) -> AbstractAsyncContextManager[AsyncGenerator[{result_type}]]:
        return {package_name.upper()}_CLIENT.subscribe(
            {result_type},
            {query.exec_source!r},
            variables={variables_str},
            headers=self.headers,
        )

                    """.strip()  # noqa: E501
                )
            else:
                query_classes.append(
                    f"""

class {class_name}(runtime.GQLOperation):
    {see_comment}
    async def execute({", ".join(args)}) -> {result_type}:
        return await {package_name.upper()}_CLIENT.query(
            {result_type},
            {query.exec_source!r},
            variables={variables_str},
            headers=self.headers,
        )

                    """.strip()
                )
        return query_classes

    def render_input_types(
        self, ordered_input_types: list[graphql.GraphQLInputObjectType]
    ) -> list[str]:
        rendered: list[str] = []
        for typ in ordered_input_types:
            if typ.is_one_of:
                rendered.extend(self._render_one_of_input_type(typ))
            else:
                rendered.append(self._render_regular_input_type(typ))
        return rendered

    def _render_regular_input_type(self, typ: graphql.GraphQLInputObjectType) -> str:
        fields: list[str] = []
        for field_name, gql_field in typ.fields.items():
            if gql_field.deprecation_reason is not None:
                _warn_deprecated_input_field(
                    typ.name, field_name, gql_field.deprecation_reason
                )
            typ_str = render_type(gql_field.type, self.scalars, enums=self.enums)
            if gql_field.default_value != graphql.Undefined:
                typ_str += f" = {gql_field.default_value!r}"
            elif not isinstance(gql_field.type, graphql.GraphQLNonNull):
                typ_str += " = None"
            fields.append(f"{self.to_snake_fn(field_name)}: {typ_str}")
        return render_pydantic_class(typ.name, "GQLModel", fields)

    def _render_one_of_input_type(
        self, typ: graphql.GraphQLInputObjectType
    ) -> list[str]:
        variant_names: list[str] = []
        rendered: list[str] = []
        for field_name, gql_field in typ.fields.items():
            if gql_field.deprecation_reason is not None:
                _warn_deprecated_input_field(
                    typ.name, field_name, gql_field.deprecation_reason
                )
            variant_name = typ.name + capitalize_first(field_name)
            variant_names.append(variant_name)
            typ_str = render_type(
                gql_field.type, self.scalars, nullable=False, enums=self.enums
            )
            fields = [f"{self.to_snake_fn(field_name)}: {typ_str}"]
            rendered.append(render_pydantic_class(variant_name, "GQLModel", fields))
        rendered.append(f"type {typ.name} = {' | '.join(variant_names)}")
        return rendered

    def render_enums(self) -> list[str]:
        result: list[str] = []
        for typ in sorted(self.enums, key=lambda t: t.name):
            for value_name, enum_value in typ.values.items():
                if enum_value.deprecation_reason is not None:
                    warnings.warn(
                        f"Enum value '{typ.name}.{value_name}' is deprecated:"
                        f" {enum_value.deprecation_reason}",
                        GraphQLDeprecationWarning,
                        stacklevel=2,
                    )
            values = ", ".join(repr(name) for name in typ.values)
            result.append(f"type {typ.name} = Literal[{values}]")
        return result


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
