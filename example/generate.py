from pathlib import Path

from pydantic import alias_generators

from iron_gql.codegen import generate_gql_package

example_dir = Path(__file__).parent

generate_gql_package(
    schema_path=example_dir / "schema.graphql",
    package_full_name="myapp.gql.api",
    base_url_import="myapp.config:GRAPHQL_URL",
    scalars={"ID": "builtins:str"},
    to_camel_fn_full_name="pydantic.alias_generators:to_camel",
    to_snake_fn=alias_generators.to_snake,
    src_path=example_dir,
)
