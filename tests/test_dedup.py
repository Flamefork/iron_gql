from pathlib import Path

from pydantic import alias_generators

from iron_gql.codegen.collect import collect_module_ir
from iron_gql.codegen.discovery import Statement
from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedModuleIR
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import ImportRef
from iron_gql.codegen.naming import apply_rename
from iron_gql.codegen.parser import parse_gql_queries


def _build_ir(
    tmp_path: Path, schema_sdl: str, query_sources: list[str]
) -> CollectedModuleIR:
    schema_path = tmp_path / "schema.graphql"
    schema_path.write_text(schema_sdl, encoding="utf-8")
    statements = [
        Statement(raw_text=text, file=Path(f"<test:{index}>"), lineno=1)
        for index, text in enumerate(query_sources)
    ]
    parsed = parse_gql_queries(schema_path, statements, [], bind_keyword_checks=())
    assert parsed.errors == []
    return apply_rename(
        collect_module_ir(
            schema=parsed.schema,
            operations=parsed.operations,
            templates=parsed.templates,
            fragment_statements=parsed.bindable_statements,
            binds=[],
            bind_keyword_checks=(),
            discovered_texts=(),
            scalars={"ID": ImportRef(module="builtins", symbol="str")},
            to_snake_fn=alias_generators.to_snake,
        ),
        frozenset(),
    )


def _models_by_graphql_type(
    artifacts: list[CollectedArtifact], graphql_type_name: str
) -> list[CollectedModel]:
    return [
        artifact
        for artifact in artifacts
        if isinstance(artifact, CollectedModel)
        and artifact.graphql_type_name == graphql_type_name
    ]


def _model_names(artifacts: list[CollectedArtifact]) -> list[str]:
    return [
        artifact.name for artifact in artifacts if isinstance(artifact, CollectedModel)
    ]


def test_within_query_same_union_variant_collapses(tmp_path: Path):
    schema = """
        type Query {
            items: ItemResult!
        }
        type ItemResult {
            primary: Item
            secondary: Item
        }
        union Item = Fruit | Vegetable
        type Fruit { name: String! }
        type Vegetable { name: String! }
    """
    query = """
        query GetItems {
            items {
                primary {
                    __typename
                    ... on Fruit { name }
                    ... on Vegetable { name }
                }
                secondary {
                    __typename
                    ... on Fruit { name }
                    ... on Vegetable { name }
                }
            }
        }
    """
    ir = _build_ir(tmp_path, schema, [query])

    fruit_models = _models_by_graphql_type(ir.result_artifacts, "Fruit")
    vegetable_models = _models_by_graphql_type(ir.result_artifacts, "Vegetable")

    assert len(fruit_models) == 1
    assert len(vegetable_models) == 1
    names = _model_names(ir.result_artifacts)
    assert names.count(fruit_models[0].name) == 1
    assert names.count(vegetable_models[0].name) == 1


def test_between_queries_same_shape_collapses(tmp_path: Path):
    schema = """
        type Query {
            feed: [Post!]!
            post(id: ID!): Post
        }
        type Post {
            id: ID!
            title: String!
        }
    """
    queries = [
        "query GetFeed { feed { id title } }",
        "query GetPost($id: ID!) { post(id: $id) { id title } }",
    ]
    ir = _build_ir(tmp_path, schema, queries)

    post_models = _models_by_graphql_type(ir.result_artifacts, "Post")
    assert len(post_models) == 1
    assert _model_names(ir.result_artifacts).count(post_models[0].name) == 1


def test_field_aliases_do_not_prevent_dedup(tmp_path: Path):
    # shape_key includes aliases, so identical aliased selections must dedup.
    schema = """
        type Query {
            left: User
            right: User
        }
        type User {
            id: ID!
            name: String!
        }
    """
    queries = [
        """
        query GetLeft {
            left { uid: id moniker: name }
        }
        """,
        """
        query GetRight {
            right { uid: id moniker: name }
        }
        """,
    ]
    ir = _build_ir(tmp_path, schema, queries)

    user_models = _models_by_graphql_type(ir.result_artifacts, "User")
    assert len(user_models) == 1
    assert _model_names(ir.result_artifacts).count(user_models[0].name) == 1


def test_union_variants_with_same_shape_across_queries_collapse(tmp_path: Path):
    schema = """
        type Query {
            a: Container!
            b: Container!
        }
        type Container {
            item: Item!
        }
        union Item = Alpha | Beta
        type Alpha { id: ID! value: String! }
        type Beta { id: ID! score: Int! }
    """
    queries = [
        """
        query GetA {
            a { item {
                __typename
                ... on Alpha { id value }
                ... on Beta { id score }
            } }
        }
        """,
        """
        query GetB {
            b { item {
                __typename
                ... on Alpha { id value }
                ... on Beta { id score }
            } }
        }
        """,
    ]
    ir = _build_ir(tmp_path, schema, queries)

    alpha_models = _models_by_graphql_type(ir.result_artifacts, "Alpha")
    beta_models = _models_by_graphql_type(ir.result_artifacts, "Beta")

    assert len(alpha_models) == 1
    assert len(beta_models) == 1

    # The alias is a shape too (ADR 0003): one alias over the same variants,
    # named after its union, and one `Container` referring to it.
    union_aliases = [
        artifact
        for artifact in ir.result_artifacts
        if isinstance(artifact, CollectedUnionAlias)
    ]
    assert [alias.name for alias in union_aliases] == ["Item"]
    assert len(_models_by_graphql_type(ir.result_artifacts, "Container")) == 1
