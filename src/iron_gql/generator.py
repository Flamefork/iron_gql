import ast
import logging
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

import graphql
from pydantic import alias_generators

from iron_gql.parser import GQLListType
from iron_gql.parser import GQLObjectType
from iron_gql.parser import GQLSingularType
from iron_gql.parser import GQLType
from iron_gql.parser import GQLVar
from iron_gql.parser import Query
from iron_gql.parser import Statement
from iron_gql.parser import collect_fragment_spreads
from iron_gql.parser import get_transitive_interfaces
from iron_gql.parser import parse_gql_queries
from iron_gql.parser import parse_input_type
from iron_gql.util import capitalize_first

logger = logging.getLogger(__name__)

type StrTransform = Callable[[str], str]

BUILTIN_SCALARS = {
    "String": "str",
    "Int": "int",
    "Float": "float",
    "Boolean": "bool",
    "Date": "datetime.date",
    "DateTime": "datetime.datetime",
    "JSON": "object",
    "Upload": "gql.FileVar",
}


@dataclass(kw_only=True)
class CollectorContext:
    enums: set[graphql.GraphQLEnumType] = dataclass_field(default_factory=set)
    input_types: set[graphql.GraphQLInputObjectType] = dataclass_field(
        default_factory=set
    )


@dataclass(kw_only=True)
class GeneratedModel:
    name: str
    fields: list[str]


@dataclass(kw_only=True)
class RenderedFieldModels:
    child_models: list[GeneratedModel] = dataclass_field(default_factory=list)
    child_model_name_base: str | None = None
    union_types: list[str] = dataclass_field(default_factory=list)
    union_field_type: GQLType | None = None


def finalize_type(typ: str, field_typ: GQLType) -> str:
    if not field_typ.not_null:
        typ += " | None"
    if field_typ.default_value != graphql.Undefined:
        typ += f" = {field_typ.default_value!r}"
    return typ


def unwrap_input_type(
    input_type: graphql.GraphQLInputType,
) -> graphql.GraphQLInputType:
    while isinstance(input_type, (graphql.GraphQLNonNull, graphql.GraphQLList)):
        input_type = input_type.of_type
    return input_type


def render_pydantic_class(name: str, base: str, fields: list[str]) -> str:
    return f"class {name}({base}):\n    {indent_block(chr(10).join(fields), '    ')}"


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
    """
    if scalars is None:
        scalars = {}

    package_name = package_full_name.split(".")[-1]  # noqa: PLC0207
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

    if parse_res.error:
        logger.error(parse_res.error)
        return False

    schema_base = schema_path.resolve()
    src_base = src_path.resolve()
    schema_for_render = schema_base.relative_to(src_base, walk_up=True)

    new_content = render_package(
        base_url_import_package=base_url_import_package,
        base_url_import_path=base_url_import_path,
        schema_path=schema_for_render,
        package_name=package_name,
        gql_fn_name=gql_fn_name,
        queries=sorted(parse_res.queries, key=lambda q: q.name),
        scalars=scalars,
        to_camel_fn_full_name=to_camel_fn_full_name,
        to_snake_fn=to_snake_fn,
    )
    changed = write_if_changed(target_package_path, new_content + "\n")
    if changed:
        logger.info(f"Generated GQL package {package_full_name}")
    return changed


def find_fn_calls(
    root_path: Path, fn_name: str, *, skip_path: Path
) -> Iterator[tuple[Path, int, ast.Call]]:
    for path in root_path.glob("**/*.py"):
        if path.is_relative_to(skip_path):
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


LINT_SUPPRESSIONS = """\
# fmt: off
# pyright: reportUnusedImport=false
# ruff: noqa: A002
# ruff: noqa: ARG001
# ruff: noqa: C901
# ruff: noqa: E303
# ruff: noqa: E501
# ruff: noqa: F401
# ruff: noqa: FBT001
# ruff: noqa: I001
# ruff: noqa: N801
# ruff: noqa: PLR0912
# ruff: noqa: PLR0913
# ruff: noqa: PLR0917
# ruff: noqa: Q000
# ruff: noqa: RUF100"""


def _render_imports(
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
from pathlib import Path
from typing import Literal
from typing import overload

import pydantic
import gql

from iron_gql import runtime

import {to_camel_module}

{scalar_imports}

from {base_url_import_package} import {base_url_symbol}"""


def _render_client_init(
    package_name: str,
    base_url_import_path: str,
    schema_path: Path,
    to_camel_fn_full_name: str,
) -> str:
    return f"""\
{package_name.upper()}_CLIENT = runtime.GQLClient(
    base_url={base_url_import_path},
    schema=Path("{schema_path}"),
)


class GQLModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        populate_by_name=True,
        alias_generator={to_camel_fn_full_name.replace(":", ".")},
        extra="forbid",
    )"""


def _render_gql_fn(
    gql_fn_name: str,
    rendered_overloads: list[str],
    query_cases: list[str],
) -> str:
    return f"""\
{"\n".join(rendered_overloads)}
@overload
def {gql_fn_name}(stmt: str) -> runtime.GQLQuery: ...


def {gql_fn_name}(stmt: str) -> runtime.GQLQuery:
    {indent_block("\n".join(query_cases), "    ")}
    return runtime.GQLQuery()"""


def render_package(
    base_url_import_package: str,
    base_url_import_path: str,
    schema_path: Path,
    package_name: str,
    gql_fn_name: str,
    queries: list[Query],
    scalars: dict[str, str],
    to_camel_fn_full_name: str,
    to_snake_fn: StrTransform,
):
    queries = get_unique_queries(queries)
    ctx = CollectorContext()

    rendered_query_classes = render_query_classes(
        queries, package_name, scalars, to_snake_fn, ctx
    )
    rendered_result_models = render_result_models(queries, scalars, to_snake_fn, ctx)
    rendered_input_types = render_input_types(
        ctx.input_types, scalars, to_snake_fn, ctx
    )
    rendered_overloads = render_overloads(queries, gql_fn_name)
    query_cases = render_query_cases(queries)
    rendered_enums = render_enums(ctx.enums)

    sections = [
        "# Code generated by iron_gql, DO NOT EDIT.",
        LINT_SUPPRESSIONS,
        _render_imports(
            to_camel_fn_full_name,
            scalars,
            base_url_import_package,
            base_url_import_path,
        ),
        _render_client_init(
            package_name, base_url_import_path, schema_path, to_camel_fn_full_name
        ),
        "\n".join(rendered_enums),
        "\n\n\n".join(rendered_result_models),
        "\n\n\n".join(rendered_input_types),
        "\n\n\n".join(rendered_query_classes),
        _render_gql_fn(gql_fn_name, rendered_overloads, query_cases),
    ]
    return "\n\n\n".join(section for section in sections if section)


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


def render_enums(enum_types: set[graphql.GraphQLEnumType]) -> list[str]:
    return [
        f"type {typ.name} = Literal[{', '.join(repr(name) for name in typ.values)}]"
        for typ in sorted(enum_types, key=lambda t: t.name)
    ]


def _extract_object_selection(
    field_type: GQLType,
) -> tuple[graphql.GraphQLNamedType, list[GQLVar]] | None:
    match field_type:
        case GQLObjectType(type=obj_type, selection=selection):
            return obj_type, selection
        case GQLListType(type=GQLObjectType(type=obj_type, selection=selection)):
            return obj_type, selection
        case _:
            return None


@dataclass(kw_only=True)
class ResultModelRenderer:
    scalars: dict[str, str]
    to_snake_fn: StrTransform
    ctx: CollectorContext
    schema: graphql.GraphQLSchema

    def render_models(
        self,
        model_name_base: str,
        fields: list[GQLVar],
        model_type: graphql.GraphQLNamedType,
        *,
        typename_type: str | None = None,
    ) -> list[GeneratedModel]:
        child_models: list[GeneratedModel] = []
        fields_mapping: dict[str, str] = {}
        for field in fields:
            rendered = self._render_field_models(model_name_base, field)
            child_models.extend(rendered.child_models)

            if field.name.startswith("__"):
                py_name = self.to_snake_fn(f"{field.name[2:]}__")
                py_alias = field.name
            else:
                py_name = self.to_snake_fn(field.name)
                py_alias = None

            py_type = self._render_field_type(
                field,
                rendered,
                model_type,
                typename_type,
            )
            if py_alias:
                py_type = f'{py_type} = pydantic.Field(alias="{field.name}")'
            fields_mapping[py_name] = py_type
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

    def _render_field_models(
        self,
        model_name_base: str,
        field: GQLVar,
    ) -> RenderedFieldModels:
        obj_info = _extract_object_selection(field.type)
        if obj_info is None:
            return RenderedFieldModels()
        obj_type, selection = obj_info
        match obj_type:
            case graphql.GraphQLObjectType():
                return self._render_object_field(
                    model_name_base,
                    field.name,
                    obj_type,
                    selection,
                )
            case graphql.GraphQLUnionType():
                return self._render_union_field(
                    model_name_base,
                    field.name,
                    obj_type,
                    selection,
                    field.type,
                )
            case graphql.GraphQLInterfaceType():
                return self._render_interface_field(
                    model_name_base,
                    field.name,
                    obj_type,
                    selection,
                    field.type,
                )
            case _:
                return RenderedFieldModels()

    def _render_object_field(
        self,
        model_name_base: str,
        field_name: str,
        obj_type: graphql.GraphQLObjectType,
        selection: list[GQLVar],
    ) -> RenderedFieldModels:
        child_model_name_base = model_name_base + capitalize_first(field_name)
        child_models = self.render_models(child_model_name_base, selection, obj_type)
        return RenderedFieldModels(
            child_models=child_models,
            child_model_name_base=child_model_name_base,
            union_types=[],
            union_field_type=None,
        )

    def _render_union_field(
        self,
        model_name_base: str,
        field_name: str,
        union_type: graphql.GraphQLUnionType,
        selection: list[GQLVar],
        field_type: GQLType,
    ) -> RenderedFieldModels:
        child_models: list[GeneratedModel] = []
        union_types: list[str] = []
        for subtyp in union_type.types:
            eligible_parents = {union_type, subtyp} | get_transitive_interfaces(subtyp)
            subtyp_sel = [
                sel_field
                for sel_field in selection
                if sel_field.parent_type in eligible_parents
            ]
            child_model_name_base = (
                model_name_base + capitalize_first(field_name) + subtyp.name
            )
            union_types.append(child_model_name_base)
            child_models.extend(
                self.render_models(child_model_name_base, subtyp_sel, subtyp)
            )
        return RenderedFieldModels(
            child_models=child_models,
            child_model_name_base=None,
            union_types=union_types,
            union_field_type=field_type,
        )

    def _render_interface_field(
        self,
        model_name_base: str,
        field_name: str,
        interface_type: graphql.GraphQLInterfaceType,
        selection: list[GQLVar],
        field_type: GQLType,
    ) -> RenderedFieldModels:
        explicit_fragment_types = self._collect_explicit_fragment_types(
            selection, interface_type
        )
        if not explicit_fragment_types:
            child_model_name_base = model_name_base + capitalize_first(field_name)
            child_models = self.render_models(
                child_model_name_base,
                selection,
                interface_type,
                typename_type="str",
            )
            return RenderedFieldModels(
                child_models=child_models,
                child_model_name_base=child_model_name_base,
                union_types=[],
                union_field_type=None,
            )

        self._require_interface_typename(selection, interface_type)
        explicit_objects = self._expand_fragment_objects(explicit_fragment_types)
        fallback_objects = (
            self._possible_interface_objects(interface_type) - explicit_objects
        )
        interface_model_base = model_name_base + capitalize_first(field_name)
        child_models, union_types = self._render_interface_union_models(
            interface_model_base,
            interface_type,
            explicit_objects,
            fallback_objects,
            selection,
        )
        return RenderedFieldModels(
            child_models=child_models,
            child_model_name_base=None,
            union_types=union_types,
            union_field_type=field_type,
        )

    def _collect_explicit_fragment_types(
        self,
        selection: list[GQLVar],
        interface_type: graphql.GraphQLInterfaceType,
    ) -> set[graphql.GraphQLNamedType]:
        return {
            sel_field.parent_type
            for sel_field in selection
            if sel_field.parent_type is not None
            and sel_field.parent_type != interface_type
        }

    def _require_interface_typename(
        self,
        selection: list[GQLVar],
        interface_type: graphql.GraphQLInterfaceType,
    ) -> None:
        has_typename = any(
            sel_field.name == "__typename" and sel_field.parent_type == interface_type
            for sel_field in selection
        )
        if not has_typename:
            msg = (
                "Missing __typename in selection set for interface "
                f"'{interface_type.name}'"
            )
            raise ValueError(msg)

    def _possible_interface_objects(
        self,
        interface_type: graphql.GraphQLInterfaceType,
    ) -> set[graphql.GraphQLObjectType]:
        return set(self.schema.get_possible_types(interface_type))

    def _expand_fragment_objects(
        self,
        fragment_types: set[graphql.GraphQLNamedType],
    ) -> set[graphql.GraphQLObjectType]:
        explicit_objects: set[graphql.GraphQLObjectType] = set()
        for fragment_type in fragment_types:
            if isinstance(fragment_type, graphql.GraphQLObjectType):
                explicit_objects.add(fragment_type)
            elif isinstance(fragment_type, graphql.GraphQLInterfaceType):
                explicit_objects.update(self._possible_interface_objects(fragment_type))
        return explicit_objects

    def _render_interface_union_models(
        self,
        interface_model_base: str,
        interface_type: graphql.GraphQLInterfaceType,
        explicit_objects: set[graphql.GraphQLObjectType],
        fallback_objects: set[graphql.GraphQLObjectType],
        selection: list[GQLVar],
    ) -> tuple[list[GeneratedModel], list[str]]:
        child_models: list[GeneratedModel] = []
        union_types: list[str] = []
        for obj_type in sorted(explicit_objects, key=lambda t: t.name):
            child_model_name_base = interface_model_base + obj_type.name
            obj_interfaces = get_transitive_interfaces(obj_type)
            eligible_parents = {interface_type, obj_type} | obj_interfaces
            obj_sel = [
                sel_field
                for sel_field in selection
                if sel_field.parent_type in eligible_parents
            ]
            child_models.extend(
                self.render_models(child_model_name_base, obj_sel, obj_type)
            )
            union_types.append(child_model_name_base)

        if fallback_objects:
            fallback_name = interface_model_base + interface_type.name
            fallback_sel = [
                sel_field
                for sel_field in selection
                if sel_field.parent_type == interface_type
            ]
            child_models.extend(
                self.render_models(
                    fallback_name,
                    fallback_sel,
                    interface_type,
                    typename_type="str",
                )
            )
            union_types.append(fallback_name)
        return child_models, union_types

    def _render_field_type(
        self,
        field: GQLVar,
        rendered: RenderedFieldModels,
        model_type: graphql.GraphQLNamedType,
        typename_type: str | None,
    ) -> str:
        if field.name == "__typename":
            return typename_type or f'Literal["{model_type.name}"]'
        if rendered.union_types:
            if rendered.union_field_type is None:
                msg = "Union field type is required for union rendering"
                raise ValueError(msg)
            return self._wrap_type(
                rendered.union_field_type, " | ".join(rendered.union_types)
            )
        return field_type(
            field.type,
            self.scalars,
            child_model_name_base=rendered.child_model_name_base,
            ctx=self.ctx,
        )

    def _wrap_type(self, field_typ: GQLType, inner: str) -> str:
        match field_typ:
            case GQLListType(type=inner_type):
                wrapped = self._wrap_type(inner_type, inner)
                typ = f"list[{wrapped}]"
            case _:
                typ = inner
        return finalize_type(typ, field_typ)


def render_result_models(
    queries: list[Query],
    scalars: dict[str, str],
    to_snake_fn: StrTransform,
    ctx: CollectorContext,
):
    if not queries:
        return []
    renderer = ResultModelRenderer(
        scalars=scalars,
        to_snake_fn=to_snake_fn,
        ctx=ctx,
        schema=queries[0].schema,
    )

    return [
        render_pydantic_class(model.name, "GQLModel", model.fields)
        for query in queries
        for model in renderer.render_models(
            f"{capitalize_first(query.name)}Result",
            query.selection_set,
            query.root_type,
        )
    ]


def render_query_classes(
    queries: list[Query],
    package_name: str,
    scalars: dict[str, str],
    to_snake_fn: StrTransform,
    ctx: CollectorContext,
) -> list[str]:
    query_classes = []
    for query in queries:
        args = ["self"]
        variables = []
        if query.variables:
            args.append("*")
        for v in query.variables:
            py_name = to_snake_fn(v.name)
            typ = field_type(v.type, scalars, ctx=ctx)
            args.append(f"{py_name}: {typ}")
            variables.append(f'"{v.name}": runtime.serialize_var({py_name})')

        referenced_fragments = _collect_referenced_fragments(query)
        stmt_doc = graphql.parse(query.stmt.clean_text)
        defined_fragments = {
            definition.name.value
            for definition in stmt_doc.definitions
            if isinstance(definition, graphql.FragmentDefinitionNode)
        }
        fragments_code = "\n".join(
            _extract_fragment_source(f)
            for f in referenced_fragments
            if f.name.value not in defined_fragments
        )
        combined_query = (
            query.stmt.raw_text + "\n" + fragments_code
            if fragments_code
            else query.stmt.raw_text
        )
        full_query_code = repr(combined_query)

        query_classes.append(
            f"""

class {capitalize_first(query.name)}(runtime.GQLQuery):
    async def execute({", ".join(args)}) -> {capitalize_first(query.name)}Result:
        request = gql.gql({full_query_code})
        request.variable_values = {{{", ".join(variables)}}} or None
        return await {package_name.upper()}_CLIENT.query(
            {capitalize_first(query.name)}Result,
            request,
            headers=self.headers,
            upload_files=self.upload_files,
        )

            """.strip()
        )
    return query_classes


def _collect_referenced_fragments(query: Query) -> list[graphql.FragmentDefinitionNode]:
    visited: set[str] = set()

    def collect_fragment_names(name: str) -> set[str]:
        if name in visited or name not in query.fragments:
            return set()
        visited.add(name)
        return {name} | {
            collected_name
            for spread in collect_fragment_spreads(query.fragments[name])
            for collected_name in collect_fragment_names(spread)
        }

    collected = {
        name
        for spread in collect_fragment_spreads(query.operation_def)
        for name in collect_fragment_names(spread)
    }

    return [query.fragments[name] for name in sorted(collected)]


def _extract_fragment_source(frag: graphql.FragmentDefinitionNode) -> str:
    if not frag.loc or not frag.loc.source:
        msg = f"Fragment {frag.name.value} has no source location"
        raise ValueError(msg)
    return frag.loc.source.body[frag.loc.start : frag.loc.end]


def render_input_types(
    collected_input_types: set[graphql.GraphQLInputObjectType],
    scalars: dict[str, str],
    to_snake_fn: StrTransform,
    ctx: CollectorContext,
) -> list[str]:
    ordered = order_input_types(collected_input_types)
    rendered: list[str] = []
    for typ in ordered:
        fields = [
            f"{to_snake_fn(field_name)}: {
                field_type(
                    parse_input_type(field.type, default_value=field.default_value),
                    scalars,
                    ctx=ctx,
                )
            }"
            for field_name, field in typ.fields.items()
        ]
        rendered.append(render_pydantic_class(typ.name, "GQLModel", fields))
    return rendered


def order_input_types(
    collected_input_types: set[graphql.GraphQLInputObjectType],
) -> list[graphql.GraphQLInputObjectType]:
    if not collected_input_types:
        return []

    _expand_input_types(collected_input_types)
    types_by_name = {typ.name: typ for typ in collected_input_types}

    emitted: set[str] = set()
    ordered: list[graphql.GraphQLInputObjectType] = []

    def emit(typ: graphql.GraphQLInputObjectType) -> None:
        if typ.name in emitted:
            return
        for field in typ.fields.values():
            target = unwrap_input_type(field.type)
            if isinstance(target, graphql.GraphQLInputObjectType):
                emit(types_by_name.get(target.name, target))
        ordered.append(typ)
        emitted.add(typ.name)

    for typ_name in sorted(types_by_name):
        emit(types_by_name[typ_name])

    return ordered


def _expand_input_types(
    collected_input_types: set[graphql.GraphQLInputObjectType],
) -> None:
    queue = list(collected_input_types)
    seen: set[graphql.GraphQLInputObjectType] = set(queue)
    while queue:
        typ = queue.pop()
        for field in typ.fields.values():
            target = unwrap_input_type(field.type)
            if isinstance(target, graphql.GraphQLInputObjectType):
                if target not in collected_input_types:
                    collected_input_types.add(target)
                if target not in seen:
                    seen.add(target)
                    queue.append(target)


def render_overloads(queries: list[Query], gql_fn_name: str) -> list[str]:
    overloads = []
    for query in queries:
        stmt = query.stmt.raw_text
        overloads.append(
            f"""

@overload
def {gql_fn_name}(stmt: Literal[{stmt!r}]) -> {capitalize_first(query.name)}: ...

            """.strip()
        )
    return overloads


def render_query_cases(queries: list[Query]) -> list[str]:
    cases = []
    for query in queries:
        stmt = query.stmt.raw_text
        cases.append(
            f"""

if stmt == {stmt!r}:
    return {capitalize_first(query.name)}()

            """.strip()
        )
    return cases


def field_type(
    field_typ: GQLType,
    scalars: dict[str, str],
    *,
    child_model_name_base: str | None = None,
    ctx: CollectorContext | None = None,
) -> str:
    match field_typ:
        case GQLSingularType():
            typ = field_py_type(field_typ.type, scalars, ctx=ctx)
        case GQLObjectType():
            if child_model_name_base is None:
                msg = "child_model_name_base must be provided for GQLObjectType"
                raise ValueError(msg)
            typ = child_model_name_base
        case GQLListType():
            child_type = field_type(
                field_typ.type,
                scalars,
                child_model_name_base=child_model_name_base,
                ctx=ctx,
            )
            typ = f"list[{child_type}]"
        case _:
            msg = f"Unknown GQLType {field_typ} of type {type(field_typ)}"
            raise TypeError(msg)
    return finalize_type(typ, field_typ)


def field_py_type(
    gql_type: graphql.GraphQLNamedType,
    scalars: dict[str, str],
    *,
    ctx: CollectorContext | None = None,
) -> str:
    match gql_type:
        case graphql.GraphQLScalarType(name=name):
            if name in scalars:
                return scalars[name].replace(":", ".")
            if name in BUILTIN_SCALARS:
                return BUILTIN_SCALARS[name]
            logger.warning(f"Unknown scalar type {name}")
            return "object"
        case graphql.GraphQLInputObjectType(name=name):
            if ctx is not None:
                ctx.input_types.add(gql_type)
            return name
        case graphql.GraphQLEnumType(name=name):
            if ctx is not None:
                ctx.enums.add(gql_type)
            return name
        case _:
            logger.warning(f"Unknown GraphQL type {gql_type.name} {type(gql_type)}")
            return "object"


def indent_block(block: str, indent: str) -> str:
    return "\n".join(
        indent + line if i > 0 and line.strip() else line
        for i, line in enumerate(block.split("\n"))
    )


def write_if_changed(path: Path, new_content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_content = path.read_text(encoding="utf-8") if path.exists() else None
    if existing_content == new_content:
        return False
    path.write_text(new_content, encoding="utf-8")
    path.touch()
    return True
