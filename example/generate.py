from pathlib import Path

from pydantic import alias_generators

from iron_gql.codegen import GenerationMode
from iron_gql.codegen import generate_gql_package

example_dir = Path(__file__).parent

PACKAGES: list[tuple[GenerationMode, str]] = [
    ("async", "gql.api"),
    ("sync", "gql.api_sync"),
]


def generate_packages() -> list[bool]:
    return [
        generate_gql_package(
            mode=mode,
            schema_path=example_dir / "schema.graphql",
            src_path=example_dir,
            package_full_name=package_full_name,
            base_url_import="example.config:GRAPHQL_URL",
            scalars={"ID": "builtins:str"},
            to_camel_fn_full_name="pydantic.alias_generators:to_camel",
            to_snake_fn=alias_generators.to_snake,
        )
        for mode, package_full_name in PACKAGES
    ]


if __name__ == "__main__":
    print("Updated GQL packages:", generate_packages())
