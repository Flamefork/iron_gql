import heapq
from collections import defaultdict
from collections.abc import Iterator
from graphlib import TopologicalSorter

from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import ListRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.ir import field_name_to_pascal


def type_tokens(typ: TypeRef) -> Iterator[str]:
    match typ:
        case ListRef(element=element):
            yield "List"
            yield from type_tokens(element)
        case NamedRef(name=name):
            yield field_name_to_pascal(name)
        case ScalarRef(name_hint=hint):
            if hint is not None:
                yield hint


def _model_type_name_tokens(model: CollectedModel) -> str:
    return "".join(
        token
        for field in sorted(model.fields, key=lambda f: f.name)
        for token in type_tokens(field.type_info)
    )


def _build_dependency_graph(
    models: list[CollectedModel],
) -> tuple[dict[str, CollectedModel], dict[str, set[str]]]:
    model_names = {model.name for model in models}
    deps: dict[str, set[str]] = {}
    by_name: dict[str, CollectedModel] = {}
    for model in models:
        by_name[model.name] = model
        deps[model.name] = {dep for dep in model.dependencies if dep in model_names}
    return by_name, deps


def topological_sort(models: list[CollectedModel]) -> list[CollectedModel]:
    if not models:
        return models
    by_name, deps = _build_dependency_graph(models)
    sorter = TopologicalSorter({name: sorted(refs) for name, refs in deps.items()})
    sorter.prepare()

    # Heap breaks ties between simultaneously-ready nodes by name for determinism;
    # TopologicalSorter.get_ready() makes no ordering guarantee on its own.
    queue = list(sorter.get_ready())
    heapq.heapify(queue)
    result: list[CollectedModel] = []
    while queue:
        name = heapq.heappop(queue)
        result.append(by_name[name])
        sorter.done(name)
        for ready_name in sorter.get_ready():
            heapq.heappush(queue, ready_name)

    return result


def _field_suffix(field_names: tuple[str, ...]) -> str:
    return "".join(field_name_to_pascal(name) for name in field_names)


def _assign_detailed_names(
    models: list[CollectedModel],
) -> tuple[dict[str, str], dict[str, tuple[CollectedField, ...]]]:
    # Walk in topological order so each model's post-rename shape_key is stable
    # when recorded. The detailed name fully encodes that shape (graphql_type +
    # field names + referenced type tokens), so shape-equivalent models always
    # receive the same placeholder name — later collapse phases stay consistent
    # without retroactive rewrites.
    rename: dict[str, str] = {}
    shapes: dict[str, tuple[CollectedField, ...]] = {}
    for model in models:
        final = model.renamed(rename)
        shapes[model.name] = final.shape_key
        detailed = (
            f"{final.graphql_type_name}"
            f"With{_field_suffix(final.field_names_key)}"
            f"_{_model_type_name_tokens(final)}"
        )
        if model.name != detailed:
            rename[model.name] = detailed
    return rename, shapes


def _collapse_single_shape_slots(
    models: list[CollectedModel],
    shapes: dict[str, tuple[CollectedField, ...]],
    rename: dict[str, str],
) -> None:
    # (graphql_type_name, field_names_key) is rename-invariant; group on it and,
    # where only one distinct post-rename shape_key lives in a slot, rewrite to
    # the short form {Type}With{FieldSuffix}. Models with identical shapes share
    # the same Phase A name, so overwriting them together is safe.
    slot_shapes: dict[tuple[str, tuple[str, ...]], set[tuple[CollectedField, ...]]] = (
        defaultdict(set)
    )
    for model in models:
        # Filtered by build_rename_map; narrow for the type checker.
        if (gql_type := model.graphql_type_name) is None:
            continue
        slot_shapes[gql_type, model.field_names_key].add(shapes[model.name])

    for model in models:
        if (gql_type := model.graphql_type_name) is None:
            continue
        slot = (gql_type, model.field_names_key)
        if len(slot_shapes[slot]) != 1:
            continue
        short = f"{gql_type}With{_field_suffix(model.field_names_key)}"
        if model.name != short:
            rename[model.name] = short


def _collapse_single_variant_types(
    rename: dict[str, str],
    artifacts: list[CollectedArtifact],
) -> None:
    # When every model of a graphql_type ends up with the same name and the bare
    # graphql_type name is not reserved by another artifact, promote to bare.
    type_variants: dict[str, set[str]] = defaultdict(set)
    for artifact in artifacts:
        if (
            isinstance(artifact, CollectedModel)
            and artifact.graphql_type_name is not None
        ):
            type_variants[artifact.graphql_type_name].add(
                rename.get(artifact.name, artifact.name)
            )

    reserved = {
        artifact.name for artifact in artifacts if artifact.name not in rename
    } | set(rename.values())

    for gql_type, variants in sorted(type_variants.items()):
        if len(variants) != 1:
            continue
        current = next(iter(variants))
        if current == gql_type or gql_type in reserved:
            continue
        for key in list(rename):
            if rename[key] == current:
                rename[key] = gql_type
        rename[current] = gql_type
        reserved.discard(current)
        reserved.add(gql_type)


def build_rename_map(artifacts: list[CollectedArtifact]) -> dict[str, str]:
    typed_models = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, CollectedModel)
        and artifact.graphql_type_name is not None
    ]
    models = topological_sort(typed_models)

    rename, shapes = _assign_detailed_names(models)
    _collapse_single_shape_slots(models, shapes, rename)
    _collapse_single_variant_types(rename, artifacts)
    return rename


def apply_rename(ir: CollectedPackageIR) -> CollectedPackageIR:
    rename = build_rename_map(ir.result_artifacts)
    renamed_result_artifacts: list[CollectedArtifact] = []
    seen_models: dict[str, CollectedModel] = {}
    for artifact in ir.result_artifacts:
        match artifact:
            case CollectedModel():
                renamed_model = artifact.renamed(rename)
                existing = seen_models.get(renamed_model.name)
                if existing is not None:
                    # Collision is only valid when models share shape — same name
                    # from two distinct shapes would be a rename-map bug.
                    if existing.shape_key != renamed_model.shape_key:
                        msg = (
                            f"rename collision on {renamed_model.name!r}:"
                            " differing shapes"
                        )
                        raise AssertionError(msg)
                    continue
                seen_models[renamed_model.name] = renamed_model
                renamed_result_artifacts.append(renamed_model)
            case CollectedUnionAlias():
                renamed_result_artifacts.append(artifact.renamed(rename))
    return CollectedPackageIR(
        result_artifacts=renamed_result_artifacts,
        input_artifacts=ir.input_artifacts,
        operations=ir.operations,
        enums=ir.enums,
    )
