from pathlib import Path

from pydantic import alias_generators

from iron_gql.codegen import generate_gql_package

example_dir = Path(__file__).parent

if __name__ == "__main__":
    changed = generate_gql_package(
        mode="async",
        schema_path=example_dir / "schema.graphql",
        src_path=example_dir,
        package_full_name="gql.api",
        base_url_import="example.config:GRAPHQL_URL",
        scalars={"ID": "builtins:str"},
        to_camel_fn_full_name="pydantic.alias_generators:to_camel",
        to_snake_fn=alias_generators.to_snake,
    )
    print("Updated GQL package:", changed)

    changed = generate_gql_package(
        mode="sync",
        schema_path=example_dir / "schema.graphql",
        src_path=example_dir,
        package_full_name="gql.api_sync",
        base_url_import="example.config:GRAPHQL_URL",
        scalars={"ID": "builtins:str"},
        to_camel_fn_full_name="pydantic.alias_generators:to_camel",
        to_snake_fn=alias_generators.to_snake,
    )
    print("Updated sync GQL package:", changed)
