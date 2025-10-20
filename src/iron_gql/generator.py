import ast
import logging
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
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
from iron_gql.parser import parse_gql_queries
from iron_gql.parser import parse_input_type
from iron_gql.util import capitalize_first

logger = logging.getLogger(__name__)

type StrTransform = Callable[[str], str]


@dataclass(kw_only=True)
class GeneratedModel:
    name: str
    fields: list[str]


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

    collected_enums: set[graphql.GraphQLEnumType] = set()
    collected_input_types: set[graphql.GraphQLInputObjectType] = set()

    rendered_query_classes = render_query_classes(
        queries,
        package_name,
        scalars,
        to_snake_fn,
        collected_input_types,
        collected_enums,
    )
    rendered_result_models = render_result_models(
        queries,
        scalars,
        to_snake_fn,
        collected_enums,
    )
    rendered_input_types = render_input_types(
        collected_input_types,
        scalars,
        to_snake_fn,
        collected_enums,
    )
    rendered_overloads = render_overloads(queries, gql_fn_name)
    query_cases = render_query_cases(queries)
    rendered_enums = render_enums(collected_enums)

    import_modules = [m.split(":")[0] for m in scalars.values()]

    return f"""

# Code generated by iron_gql, DO NOT EDIT.

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
# ruff: noqa: RUF100

import datetime
from pathlib import Path
from typing import IO
from typing import Literal
from typing import overload

import pydantic
import gql

from iron_gql import runtime

import {to_camel_fn_full_name.split(":", maxsplit=1)[0]}

{"\n".join(f"import {m}" for m in import_modules)}

from {base_url_import_package} import {base_url_import_path.split(".", maxsplit=1)[0]}

{package_name.upper()}_CLIENT = runtime.GQLClient(
    base_url={base_url_import_path},
    schema=Path("{schema_path}"),
)


class GQLModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(
        populate_by_name=True,
        alias_generator={to_camel_fn_full_name.replace(":", ".")},
        extra="forbid",
    )


{"\n".join(rendered_enums)}


{"\n\n\n".join(rendered_result_models)}


{"\n\n\n".join(rendered_input_types)}


{"\n\n\n".join(rendered_query_classes)}


{"\n".join(rendered_overloads)}
@overload
def {gql_fn_name}(stmt: str) -> runtime.GQLQuery: ...


def {gql_fn_name}(stmt: str) -> runtime.GQLQuery:
    {indent_block("\n".join(query_cases), "    ")}
    return runtime.GQLQuery()

    """.strip()


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


def render_result_models(
    queries: list[Query],
    scalars: dict[str, str],
    to_snake_fn: StrTransform,
    collect_enum_types: set[graphql.GraphQLEnumType],
):
    def get_result_models(
        model_name_base: str,
        fields: list[GQLVar],
        model_type: graphql.GraphQLNamedType,
    ) -> list[GeneratedModel]:
        child_models: list[GeneratedModel] = []
        fields_mapping = {}
        for field in fields:
            child_model_name_base = None
            union_types: list[str] = []
            match field.type:
                case (
                    GQLObjectType(type=obj_type, selection=sel)
                    | GQLListType(type=GQLObjectType(type=obj_type, selection=sel))
                ) if isinstance(obj_type, graphql.GraphQLObjectType):
                    child_model_name_base = (
                        model_name_base + capitalize_first(field.name) + ""
                    )
                    child_models.extend(
                        get_result_models(child_model_name_base, sel, obj_type)
                    )
                case (
                    GQLObjectType(type=union_type, selection=sel)
                    | GQLListType(type=GQLObjectType(type=union_type, selection=sel))
                ) if isinstance(union_type, graphql.GraphQLUnionType):
                    for subtyp in union_type.types:
                        subtyp_sel = [
                            field
                            for field in sel
                            if field.parent_type in {subtyp, union_type}
                        ]
                        child_model_name_base = (
                            model_name_base + capitalize_first(field.name) + subtyp.name
                        )
                        union_types.append(child_model_name_base)
                        child_models.extend(
                            get_result_models(child_model_name_base, subtyp_sel, subtyp)
                        )
                case _:
                    pass

            if field.name.startswith("__"):
                py_name = to_snake_fn(f"{field.name[2:]}__")
                py_alias = field.name
            else:
                py_name = to_snake_fn(field.name)
                py_alias = None
            if field.name == "__typename":
                py_type = f'Literal["{model_type.name}"]'
            elif union_types:
                py_type = " | ".join(union_types)
            else:
                py_type = field_type(
                    field.type,
                    scalars,
                    child_model_name_base=child_model_name_base,
                    collect_enum_types=collect_enum_types,
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

    return [
        f"""

class {model.name}(GQLModel):
    {indent_block("\n".join(model.fields), "    ")}

        """.strip()
        for query in queries
        for model in get_result_models(
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
    collected_input_types: set[graphql.GraphQLInputObjectType],
    collect_enum_types: set[graphql.GraphQLEnumType],
) -> list[str]:
    query_classes = []
    for query in queries:
        args = ["self"]
        variables = []
        if query.variables:
            args.append("*")
        for v in query.variables:
            py_name = to_snake_fn(v.name)
            typ = field_type(
                v.type,
                scalars,
                collect_input_types=collected_input_types,
                collect_enum_types=collect_enum_types,
            )
            args.append(f"{py_name}: {typ}")
            variables.append(f'"{v.name}": runtime.serialize_var({py_name})')
        query_classes.append(
            f"""

class {capitalize_first(query.name)}(runtime.GQLQuery):
    async def execute({", ".join(args)}) -> {capitalize_first(query.name)}Result:
        document = gql.gql({query.stmt.raw_text!r})
        return await {package_name.upper()}_CLIENT.query(
            {capitalize_first(query.name)}Result,
            document,
            {{{", ".join(variables)}}},
            headers=self.headers,
            upload_files=self.upload_files,
        )

            """.strip()
        )
    return query_classes


def render_input_types(
    collected_input_types: set[graphql.GraphQLInputObjectType],
    scalars: dict[str, str],
    to_snake_fn: StrTransform,
    collect_enum_types: set[graphql.GraphQLEnumType],
) -> list[str]:
    ordered = order_input_types(collected_input_types)
    rendered: list[str] = []
    for typ in ordered:
        fields = [
            f"{to_snake_fn(field_name)}: {
                field_type(
                    parse_input_type(field.type, default_value=field.default_value),
                    scalars,
                    collect_input_types=collected_input_types,
                    collect_enum_types=collect_enum_types,
                )
            }"
            for field_name, field in typ.fields.items()
        ]
        rendered.append(
            f"""

class {typ.name}(GQLModel):
    {indent_block("\n".join(fields), "    ")}

            """.strip()
        )
    return rendered


def order_input_types(  # noqa: C901
    collected_input_types: set[graphql.GraphQLInputObjectType],
) -> list[graphql.GraphQLInputObjectType]:
    if not collected_input_types:
        return []

    def unwrap_input_type(
        input_type: graphql.GraphQLInputType,
    ) -> graphql.GraphQLInputType:
        while isinstance(input_type, (graphql.GraphQLNonNull, graphql.GraphQLList)):
            input_type = input_type.of_type
        return input_type

    def expand_types() -> None:
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

    expand_types()

    emitted: set[str] = set()
    ordered: list[graphql.GraphQLInputObjectType] = []
    types_by_name = {typ.name: typ for typ in collected_input_types}

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


def render_ordered_input_types(
    ordered_types: list[graphql.GraphQLInputObjectType],
    scalars: dict[str, str],
    to_snake_fn: StrTransform,
    collect_enum_types: set[graphql.GraphQLEnumType],
    collected_input_types: set[graphql.GraphQLInputObjectType],
) -> list[str]:
    rendered: list[str] = []
    for typ in ordered_types:
        fields = [
            f"{to_snake_fn(field_name)}: {
                field_type(
                    parse_input_type(field.type, default_value=field.default_value),
                    scalars,
                    collect_input_types=collected_input_types,
                    collect_enum_types=collect_enum_types,
                )
            }"
            for field_name, field in typ.fields.items()
        ]
        rendered.append(
            f"""

class {typ.name}(GQLModel):
    {indent_block("\n".join(fields), "    ")}

            """.strip()
        )
    return rendered


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
    collect_input_types: set[graphql.GraphQLInputObjectType] | None = None,
    collect_enum_types: set[graphql.GraphQLEnumType] | None = None,
) -> str:
    match field_typ:
        case GQLSingularType():
            typ = field_py_type(
                field_typ.type,
                scalars,
                collect_input_types=collect_input_types,
                collect_enum_types=collect_enum_types,
            )
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
                collect_input_types=collect_input_types,
                collect_enum_types=collect_enum_types,
            )
            typ = f"list[{child_type}]"
        case _:
            msg = f"Unknown GQLType {field_typ} of type {type(field_typ)}"
            raise TypeError(msg)

    if not field_typ.not_null:
        typ += " | None"

    if field_typ.default_value != graphql.Undefined:
        typ += f" = {field_typ.default_value!r}"

    return typ


def field_py_type(  # noqa: C901, PLR0912
    gql_type: graphql.GraphQLNamedType,
    scalars: dict[str, str],
    *,
    collect_input_types: set[graphql.GraphQLInputObjectType] | None,
    collect_enum_types: set[graphql.GraphQLEnumType] | None = None,
) -> str:
    match gql_type:
        case graphql.GraphQLScalarType(name=name) if name in scalars:
            return scalars[name].replace(":", ".")
        case graphql.GraphQLScalarType(name="String"):
            return "str"
        case graphql.GraphQLScalarType(name="Int"):
            return "int"
        case graphql.GraphQLScalarType(name="Float"):
            return "float"
        case graphql.GraphQLScalarType(name="Boolean"):
            return "bool"
        case graphql.GraphQLScalarType(name="Date"):
            return "datetime.date"
        case graphql.GraphQLScalarType(name="DateTime"):
            return "datetime.datetime"
        case graphql.GraphQLScalarType(name="JSON"):
            return "object"
        case graphql.GraphQLScalarType(name="Upload"):
            return "IO"
        case graphql.GraphQLInputObjectType(name=name):
            if collect_input_types is not None:
                collect_input_types.add(gql_type)
            return name
        case graphql.GraphQLEnumType(name=name):
            if collect_enum_types is not None:
                collect_enum_types.add(gql_type)
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
