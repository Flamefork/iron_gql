from pathlib import Path

from pydantic import alias_generators

from iron_gql.codegen import GenerationMode
from iron_gql.codegen import RenderedModule
from iron_gql.codegen import render_gql_module

example_dir = Path(__file__).parent

MODULES: list[tuple[GenerationMode, str]] = [
    ("async", "gql.api"),
    ("sync", "gql.api_sync"),
]


def render_modules() -> list[RenderedModule]:
    return [
        render_gql_module(
            mode=mode,
            schema_path=example_dir / "schema.graphql",
            src_path=example_dir,
            module_full_name=module_full_name,
            base_url_import="example.config:GRAPHQL_URL",
            scalars={"ID": "builtins:str"},
            to_camel_fn_full_name="pydantic.alias_generators:to_camel",
            to_snake_fn=alias_generators.to_snake,
        )
        for mode, module_full_name in MODULES
    ]


if __name__ == "__main__":
    for rendered in render_modules():
        rendered.write()
