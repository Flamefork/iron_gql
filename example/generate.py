from pathlib import Path

from pydantic import alias_generators

from iron_gql.codegen import generate_gql_package

example_dir = Path(__file__).parent
schema_path = example_dir / "schema.graphql"
src_path = example_dir


def generate_gql_example(schema_path: Path, src_path: Path) -> bool:
    return generate_gql_package(
        schema_path=schema_path,
        package_full_name="gql.api",
        base_url_import="example.config:GRAPHQL_URL",
        scalars={"ID": "builtins:str"},
        to_camel_fn_full_name="pydantic.alias_generators:to_camel",
        to_snake_fn=alias_generators.to_snake,
        src_path=src_path,
    )


if __name__ == "__main__":
    changed = generate_gql_example(schema_path, src_path)
    print("Updated GQL package:", changed)
