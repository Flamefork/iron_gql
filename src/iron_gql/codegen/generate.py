import ast
from collections.abc import Iterator
from pathlib import Path

from pydantic import alias_generators

from iron_gql.codegen.collect import collect_package_ir
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ImportRef
from iron_gql.codegen.ir import StrTransform
from iron_gql.codegen.naming import apply_rename
from iron_gql.codegen.parser import Statement
from iron_gql.codegen.parser import parse_gql_queries
from iron_gql.codegen.render import render_package
from iron_gql.codegen.util import write_if_changed


def _find_fn_calls(
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


def _find_all_queries(
    src_path: Path, gql_fn_name: str, *, skip_path: Path
) -> Iterator[Statement]:
    for file, lineno, node in _find_fn_calls(
        src_path, gql_fn_name, skip_path=skip_path
    ):
        relative_path = file.relative_to(src_path)

        if (
            len(node.args) != 1
            or not isinstance(node.args[0], ast.Constant)
            or not isinstance(node.args[0].value, str)
        ):
            msg = (
                f"Invalid positional arguments for {gql_fn_name} "
                f"at {relative_path}:{lineno}, "
                "expected a single string literal"
            )
            raise TypeError(msg)

        yield Statement(raw_text=node.args[0].value, file=relative_path, lineno=lineno)


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
    base_url_ref = ImportRef.parse(base_url_import)
    scalar_refs = {name: ImportRef.parse(ref) for name, ref in scalars.items()}
    to_camel_ref = ImportRef.parse(to_camel_fn_full_name)

    queries = list(
        _find_all_queries(src_path, gql_fn_name, skip_path=target_package_path)
    )

    parse_res = parse_gql_queries(
        schema_path,
        queries,
        debug_path=debug_path,
    )

    if parse_res.errors:
        raise GraphQLGenerationError(parse_res.errors)

    collected = apply_rename(
        collect_package_ir(
            queries=parse_res.queries,
            scalars=scalar_refs,
            to_snake_fn=to_snake_fn,
        )
    )
    new_content = render_package(
        base_url_ref=base_url_ref,
        package_name=package_name,
        gql_fn_name=gql_fn_name,
        collected=collected,
        scalars=scalar_refs,
        to_camel_ref=to_camel_ref,
    )
    return write_if_changed(target_package_path, new_content + "\n")
