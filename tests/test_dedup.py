from pathlib import Path

import graphql
from pydantic import alias_generators

from iron_gql.codegen.collect import collect_package_ir
from iron_gql.codegen.discovery import Statement
from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import ImportRef
from iron_gql.codegen.naming import apply_rename
from iron_gql.codegen.parser import build_queries
from iron_gql.codegen.parser import classify_queries
from iron_gql.codegen.parser import collect_fragment_statements
from iron_gql.codegen.parser import collect_fragments
from iron_gql.codegen.parser import parse_documents


def _build_ir(schema_sdl: str, query_sources: list[str]) -> CollectedPackageIR:
    schema = graphql.build_schema(schema_sdl)
    statements = [
        Statement(raw_text=text, file=Path(f"<test:{index}>"), lineno=1)
        for index, text in enumerate(query_sources)
    ]
    docs = parse_documents(statements)
    fragments = collect_fragments(docs)
    operations, templates, errors = classify_queries(
        build_queries(schema, docs, fragments)
    )
    assert errors == []
    return apply_rename(
        collect_package_ir(
            schema=schema,
            operations=operations,
            templates=templates,
            fragment_statements=collect_fragment_statements(schema, docs, fragments),
            binds=[],
            bind_keyword_checks=(),
            discovered_texts=(),
            scalars={"ID": ImportRef.parse("builtins:str")},
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


def test_within_query_same_union_variant_collapses():
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
    ir = _build_ir(schema, [query])

    fruit_models = _models_by_graphql_type(ir.result_artifacts, "Fruit")
    vegetable_models = _models_by_graphql_type(ir.result_artifacts, "Vegetable")

    assert len(fruit_models) == 1
    assert len(vegetable_models) == 1
    names = _model_names(ir.result_artifacts)
    assert names.count(fruit_models[0].name) == 1
    assert names.count(vegetable_models[0].name) == 1


def test_between_queries_same_shape_collapses():
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
    ir = _build_ir(schema, queries)

    post_models = _models_by_graphql_type(ir.result_artifacts, "Post")
    assert len(post_models) == 1
    assert _model_names(ir.result_artifacts).count(post_models[0].name) == 1


def test_field_aliases_do_not_prevent_dedup():
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
    ir = _build_ir(schema, queries)

    user_models = _models_by_graphql_type(ir.result_artifacts, "User")
    assert len(user_models) == 1
    assert _model_names(ir.result_artifacts).count(user_models[0].name) == 1


def test_union_variants_with_same_shape_across_queries_collapse():
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
    ir = _build_ir(schema, queries)

    alpha_models = _models_by_graphql_type(ir.result_artifacts, "Alpha")
    beta_models = _models_by_graphql_type(ir.result_artifacts, "Beta")

    assert len(alpha_models) == 1
    assert len(beta_models) == 1

    union_aliases = [
        artifact
        for artifact in ir.result_artifacts
        if isinstance(artifact, CollectedUnionAlias)
    ]
    assert len(union_aliases) == 2
    assert union_aliases[0].variants == union_aliases[1].variants
