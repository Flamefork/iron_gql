from pathlib import Path

from pydantic import alias_generators

from iron_gql.codegen.collect import collect_package_ir
from iron_gql.codegen.collect import parametrize_slot_paths
from iron_gql.codegen.discovery import discover_package
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ImportRef
from iron_gql.codegen.ir import StrTransform
from iron_gql.codegen.naming import apply_rename
from iron_gql.codegen.naming import validate_module_names
from iron_gql.codegen.naming import validate_signature_names
from iron_gql.codegen.parser import parse_gql_queries
from iron_gql.codegen.parser import write_ignored_binds
from iron_gql.codegen.render import GenerationMode
from iron_gql.codegen.render import render_package
from iron_gql.codegen.render import scaffold_claims
from iron_gql.codegen.slots import validate_no_nested_slots
from iron_gql.codegen.util import write_if_changed


# Generates a typed GraphQL client from schema_path and the api_gql() calls
# discovered under src_path: a module at package_full_name with Pydantic
# models and typed operation classes. Returns True when the generated file
# changed. A diagnosed rejection of the GraphQL input raises
# GraphQLGenerationError; a malformed api_gql call site (anything but a single
# string literal) raises TypeError before any GraphQL is read.
def generate_gql_package(
    *,
    mode: GenerationMode,
    schema_path: Path,
    src_path: Path,
    package_full_name: str,
    base_url_import: str,
    scalars: dict[str, str] | None = None,
    to_camel_fn_full_name: str = "pydantic.alias_generators:to_camel",
    to_snake_fn: StrTransform = alias_generators.to_snake,
    debug_path: Path | None = None,
) -> bool:
    if scalars is None:
        scalars = {}

    package_name = package_full_name.rsplit(".", maxsplit=1)[-1]
    gql_fn_name = f"{package_name}_gql"

    target_package_path = src_path / f"{package_full_name.replace('.', '/')}.py"
    base_url_ref = ImportRef.parse(base_url_import)
    scalar_refs = {name: ImportRef.parse(ref) for name, ref in scalars.items()}
    to_camel_ref = ImportRef.parse(to_camel_fn_full_name)

    discovered = discover_package(src_path, gql_fn_name, skip_path=target_package_path)

    if debug_path is not None:
        # Written before anything can reject the GraphQL: a bind that went
        # missing is exactly what a debug run of a package that does not
        # generate is looking for.
        write_ignored_binds(debug_path, discovered.ignored)

    parse_res = parse_gql_queries(
        schema_path,
        discovered.statements,
        discovered.binds,
        debug_path=debug_path,
    )

    if parse_res.errors:
        raise GraphQLGenerationError(parse_res.errors)

    scaffold = scaffold_claims(
        package_name=package_name,
        gql_fn_name=gql_fn_name,
        base_url_ref=base_url_ref,
        scalars=scalar_refs,
        to_camel_ref=to_camel_ref,
    )
    collected = apply_rename(
        collect_package_ir(
            schema=parse_res.schema,
            operations=parse_res.operations,
            templates=parse_res.templates,
            fragment_statements=parse_res.reachable_statements,
            binds=discovered.binds,
            discovered_texts=tuple(stmt.raw_text for stmt in discovered.statements),
            scalars=scalar_refs,
            to_snake_fn=to_snake_fn,
        ),
        frozenset(scaffold),
    )
    # Checked here rather than in the parser: these rules read the collected
    # module — the python names it binds, which are only final once the rename
    # pass has run, and the model graph, which has already merged the field
    # nodes a response key was assembled from.
    #
    # The nested-slot rule is asked before parametrisation and the other two
    # after, because they read different graphs: nesting is a fact about a
    # template's own result subtree, read from the references between models,
    # and parametrisation rewrites those references to carry the slot phantoms
    # down the path.
    nested_slot_errors = validate_no_nested_slots(collected)
    collected = parametrize_slot_paths(collected)
    ir_errors = [
        *validate_module_names(collected, scaffold),
        *validate_signature_names(collected),
        *nested_slot_errors,
    ]
    if ir_errors:
        raise GraphQLGenerationError(ir_errors)

    new_content = render_package(
        mode=mode,
        base_url_ref=base_url_ref,
        package_name=package_name,
        gql_fn_name=gql_fn_name,
        collected=collected,
        scalars=scalar_refs,
        to_camel_ref=to_camel_ref,
    )
    return write_if_changed(target_package_path, new_content + "\n")
