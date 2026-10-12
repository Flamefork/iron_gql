import dataclasses
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import alias_generators

from iron_gql.codegen.collect import collect_module_ir
from iron_gql.codegen.collect import parametrize_slot_paths
from iron_gql.codegen.discovery import discover_module
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ImportRef
from iron_gql.codegen.ir import StrTransform
from iron_gql.codegen.naming import apply_rename
from iron_gql.codegen.naming import validate_module_names
from iron_gql.codegen.naming import validate_signature_names
from iron_gql.codegen.parser import parse_gql_queries
from iron_gql.codegen.parser import write_ignored_binds
from iron_gql.codegen.parser import write_skipped_dirs
from iron_gql.codegen.render import GenerationMode
from iron_gql.codegen.render import render_module
from iron_gql.codegen.render import scaffold_claims
from iron_gql.codegen.slots import validate_no_nested_slots
from iron_gql.runtime import SubscriptionProtocol


@dataclass(frozen=True)
class RenderedModule:
    path: Path
    text: str = dataclasses.field(repr=False)

    def write(self) -> None:
        if self._disk_text() == self.text:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.text, encoding="utf-8", newline="")

    def diff(self) -> str:
        disk_text = self._disk_text()
        if disk_text == self.text:
            return ""
        if disk_text is None and not self.text:
            # Neither side has a line to compare, yet write() creates the file.
            return f"--- /dev/null\n+++ {self.path}\n"
        lines = difflib.unified_diff(
            [] if disk_text is None else _diff_lines(disk_text),
            _diff_lines(self.text),
            fromfile="/dev/null" if disk_text is None else str(self.path),
            tofile=str(self.path),
        )
        # unified_diff emits a last line without its newline as is, so the next
        # diff line is glued to it and the difference is invisible.
        return "".join(
            line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n"
            for line in lines
        )

    def _disk_text(self) -> str | None:
        # newline="" keeps CRLF as written: read_text would translate it and
        # report a CRLF file as current.
        try:
            return self.path.read_text(encoding="utf-8", newline="")
        except FileNotFoundError:
            return None


def _diff_lines(text: str) -> list[str]:
    # str.splitlines also breaks at \r, \x0c, \u2028 and the like, which can stand
    # inside a string literal of the module; only \n ends a line of the written file.
    return re.findall(r"[^\n]*\n|[^\n]+\Z", text)


# Renders a typed GraphQL client from schema_path and the api_gql() calls
# discovered under src_path: a module at module_full_name with Pydantic models
# and typed operation classes. Nothing is written but the debug_path dump;
# write() puts the module in place and diff() compares it with the file. A
# diagnosed rejection of the GraphQL input raises GraphQLGenerationError; a
# malformed api_gql call site (anything but a single string literal) raises
# TypeError before any GraphQL is read.
def render_gql_module(
    *,
    mode: GenerationMode,
    schema_path: Path,
    src_path: Path,
    module_full_name: str,
    base_url_import: str,
    subscription_protocol: SubscriptionProtocol,
    scalars: dict[str, str] | None = None,
    to_camel_fn_full_name: str = "pydantic.alias_generators:to_camel",
    to_snake_fn: StrTransform = alias_generators.to_snake,
    debug_path: Path | None = None,
) -> RenderedModule:
    if scalars is None:
        scalars = {}

    module_name = module_full_name.rsplit(".", maxsplit=1)[-1]
    gql_fn_name = f"{module_name}_gql"

    target_module_path = src_path / f"{module_full_name.replace('.', '/')}.py"
    base_url_ref = ImportRef.parse(base_url_import)
    scalar_refs = {name: ImportRef.parse(ref) for name, ref in scalars.items()}
    to_camel_ref = ImportRef.parse(to_camel_fn_full_name)

    discovered = discover_module(src_path, gql_fn_name, skip_path=target_module_path)

    if debug_path is not None:
        # Written before anything can reject the GraphQL: a bind that went
        # missing is exactly what a debug run of a module that does not
        # generate is looking for. A statement that went missing with the whole
        # directory it stood in is the same question one level up.
        write_ignored_binds(debug_path, discovered.ignored)
        write_skipped_dirs(debug_path, discovered.skipped)

    parse_res = parse_gql_queries(
        schema_path,
        discovered.statements,
        discovered.binds,
        bind_keyword_checks=discovered.bind_keyword_checks,
        debug_path=debug_path,
    )

    if parse_res.errors:
        raise GraphQLGenerationError(parse_res.errors)

    scaffold = scaffold_claims(
        gql_fn_name=gql_fn_name,
        base_url_ref=base_url_ref,
        scalars=scalar_refs,
        to_camel_ref=to_camel_ref,
    )
    collected = apply_rename(
        collect_module_ir(
            schema=parse_res.schema,
            operations=parse_res.operations,
            templates=parse_res.templates,
            fragment_statements=parse_res.bindable_statements,
            binds=discovered.binds,
            bind_keyword_checks=discovered.bind_keyword_checks,
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

    new_content = render_module(
        mode=mode,
        base_url_ref=base_url_ref,
        subscription_protocol=subscription_protocol,
        gql_fn_name=gql_fn_name,
        collected=collected,
        scalars=scalar_refs,
        to_camel_ref=to_camel_ref,
    )
    return RenderedModule(target_module_path, new_content + "\n")
