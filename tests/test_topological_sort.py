from iron_gql.codegen.generator import GeneratedModel
from iron_gql.codegen.generator import topological_sort


def test_topological_sort_keeps_global_lexicographic_order_for_ready_models():
    models = [
        GeneratedModel(
            name="Delta",
            fields=["beta: Beta", "gamma: Gamma"],
            graphql_type_name="Delta",
        ),
        GeneratedModel(name="Gamma", fields=[], graphql_type_name="Gamma"),
        GeneratedModel(name="Alpha", fields=[], graphql_type_name="Alpha"),
        GeneratedModel(
            name="Beta",
            fields=["alpha: Alpha"],
            graphql_type_name="Beta",
        ),
    ]

    ordered = topological_sort(models)

    assert [model.name for model in ordered] == ["Alpha", "Beta", "Gamma", "Delta"]
