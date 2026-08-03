import heapq
import keyword
from collections import defaultdict
from collections.abc import Iterator
from collections.abc import Mapping
from graphlib import TopologicalSorter

from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ListRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.ir import field_name_to_pascal
from iron_gql.codegen.slots import fragment_base_name


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
    sorter = TopologicalSorter(deps)
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
    occupied: frozenset[str],
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
        # Both collapse phases yield to the same occupied set: a short name a
        # slot model, a pinned fragment model, an operation or the scaffold
        # already holds stays with its detailed Phase A name instead.
        if short in occupied:
            continue
        if model.name != short:
            rename[model.name] = short


def _collapse_single_variant_types(
    rename: dict[str, str],
    renamable: list[CollectedModel],
    occupied: frozenset[str],
) -> None:
    # When every model of a graphql_type ends up with the same name and the bare
    # graphql_type name is not reserved by another artifact, promote to bare.
    # Only renamable models get a vote: a model whose name is fixed cannot be
    # promoted, and counting it here would either block a promotion it has no
    # stake in or — worse — rewrite the very name that was meant to stay put.
    type_variants: dict[str, set[str]] = defaultdict(set)
    for model in renamable:
        # Filtered by build_rename_map; narrow for the type checker.
        if (gql_type := model.graphql_type_name) is None:
            continue
        type_variants[gql_type].add(rename.get(model.name, model.name))

    reserved = set(occupied) | set(rename.values())

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


def build_rename_map(
    artifacts: list[CollectedArtifact],
    pinned_names: frozenset[str],
    reserved_names: frozenset[str],
) -> dict[str, str]:
    # Slot models are excluded: the collapse phases merge models that share a
    # GraphQL type and field set, and two slots with the same static selection
    # (`{ __typename }` alone is the common case) would end up as one class
    # whose single `slot_name__` could only name one of them.
    # Fragment models — the root and everything reachable from it, the types a
    # caller writes and narrows against after `read` — are excluded for a
    # different reason: a handle's names are public API, so they may not
    # depend on which other operations the package contains.
    typed_models = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, CollectedModel)
        and artifact.graphql_type_name is not None
        and artifact.slot_name is None
        and artifact.name not in pinned_names
    ]
    models = topological_sort(typed_models)

    rename, shapes = _assign_detailed_names(models)
    # Names no phase may take: everything that will not move — pinned fragment
    # models, slot models and other fixed artifacts (not in the rename map) —
    # plus the module names and scaffold passed in. Computed once so both
    # collapse phases yield to the same set.
    occupied = frozenset(
        {artifact.name for artifact in artifacts if artifact.name not in rename}
        | reserved_names
        | pinned_names
    )
    _collapse_single_shape_slots(models, shapes, rename, occupied)
    _collapse_single_variant_types(rename, models, occupied)
    collisions = sorted(set(rename.values()) & occupied)
    if collisions:
        names = ", ".join(f"'{name}'" for name in collisions)
        msg = (
            f"Generated model name(s) {names} collide with a fixed name "
            "(an operation, fragment, slot model or the scaffold); alias the "
            "colliding field or rename the fragment"
        )
        raise GraphQLGenerationError([msg])
    return rename


def fixed_module_names(ir: CollectedPackageIR) -> frozenset[str]:
    # The names this IR pins: an operation class, a fragment handle, that
    # handle's singleton and a slot compatibility base are each named after
    # something the developer wrote, so none of them can move to resolve a
    # clash. A model can, which is why these are fed to the rename pass as
    # reserved and only the leftovers become errors. Two more sets are pinned
    # elsewhere: fragment data models via the rename pass's pinned names, and
    # slot models by their exclusion from it. The module also binds the
    # scaffold's own names — the client, the base models, the dispatch dicts and
    # every import — which the IR knows nothing about; `render.scaffold_claims`
    # supplies those and both sets are used together everywhere.
    return frozenset(
        {operation.class_name for operation in ir.operations}
        | {fragment.class_name for fragment in ir.fragments}
        | {fragment.singleton_name for fragment in ir.fragments}
        | {fragment_base_name(slot_type) for slot_type in ir.slot_types}
    )


def apply_rename(
    ir: CollectedPackageIR, scaffold_names: frozenset[str]
) -> CollectedPackageIR:
    rename = build_rename_map(
        ir.result_artifacts,
        # Open models are pinned wholesale: beyond a handle's names being
        # public API, an open (extra="ignore") model must never converge with
        # a strict one under a shared short name.
        ir.open_model_names,
        fixed_module_names(ir) | scaffold_names,
    )
    renamed_result_artifacts: list[CollectedArtifact] = []
    seen_models: dict[str, CollectedModel] = {}
    for artifact in ir.result_artifacts:
        match artifact:
            case CollectedModel():
                renamed_model = artifact.renamed(rename)
                existing = seen_models.get(renamed_model.name)
                if existing is not None:
                    # Raw-name twins are rejected upfront by
                    # `validate_collected_names`, so a collision here can only
                    # come from the rename map converging two artifacts —
                    # legal exactly for identical shapes of the same slot,
                    # anything else is a rename-map bug.
                    if (
                        existing.shape_key != renamed_model.shape_key
                        or existing.slot_name != renamed_model.slot_name
                    ):
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
        fragments=ir.fragments,
        slot_types=ir.slot_types,
        enums=ir.enums,
        # Open-model names pass through untouched: every one of them is pinned
        # out of the rename map.
        open_model_names=ir.open_model_names,
    )


def _module_name_claims(
    ir: CollectedPackageIR, scaffold: Mapping[str, tuple[str, ...]]
) -> Iterator[tuple[str, str]]:
    for name in sorted(scaffold):
        for origin in scaffold[name]:
            yield name, origin
    for operation in ir.operations:
        at = ", ".join(operation.locations)
        yield operation.class_name, f"operation '{operation.class_name}' at {at}"
    for fragment in ir.fragments:
        origin = f"fragment '{fragment.fragment_name}' at {fragment.location}"
        yield fragment.class_name, origin
        yield fragment.singleton_name, f"the singleton of {origin}"
    for slot_type in ir.slot_types:
        yield fragment_base_name(slot_type), "a slot compatibility base"
    for artifact in (*ir.result_artifacts, *ir.input_artifacts):
        yield artifact.name, f"model '{artifact.name}'"
    for enum in ir.enums:
        yield enum.name, f"enum '{enum.name}'"


def validate_module_names(
    ir: CollectedPackageIR, scaffold: Mapping[str, tuple[str, ...]]
) -> list[str]:
    # Operation classes, fragment handles, their singletons, models, the
    # `{Type}Fragment` bases and the scaffold the module is built on (client,
    # base models, dispatch dicts, imports) all land in one namespace, and
    # Python binds the last one written. Every rebinding here breaks something
    # silently — dispatch resolving to a handle, a base class replaced by a
    # model, `API_CLIENT` replaced by a fragment singleton — so any overlap is
    # an error. Scaffold claims carry the origin of each binding, so two
    # scaffold sources fighting over one name are an overlap like any other.
    claims: dict[str, list[str]] = defaultdict(list)
    for name, origin in _module_name_claims(ir, scaffold):
        claims[name].append(origin)
    errors: list[str] = []
    for name, origins in sorted(claims.items()):
        if len(origins) > 1:
            message = f"Name '{name}' is claimed by {' and by '.join(origins)}"
            if any(origin.startswith("the singleton of") for origin in origins):
                message += (
                    "; rename the fragment so its class and singleton names differ"
                )
            errors.append(message)
        # Every claim becomes a module-level binding, so it must survive
        # `compile()` — a keyword or non-identifier would only surface as a
        # SyntaxError when the generated module is imported.
        if not name.isidentifier() or keyword.iskeyword(name):
            errors.append(
                f"Name '{name}' ({origins[0]}) is not a usable Python identifier"
            )
    return errors


def validate_execute_signatures(ir: CollectedPackageIR) -> list[str]:
    # Variables and slots share the keyword namespace of `execute`, and both
    # arrive there through `to_snake_fn` — so names that differ in GraphQL can
    # still render the same parameter twice. That source passes `ast.parse` and
    # only fails at `compile()`, which nothing downstream of codegen runs.
    errors: list[str] = []
    for operation in ir.operations:
        claims: dict[str, list[str]] = defaultdict(list)
        # `execute` takes its receiver positionally under a name that shares
        # this namespace, so a variable or slot snaking to `self` renders the
        # parameter twice just as surely as two variables would.
        claims["self"].append("the method receiver")
        if operation.slots:
            # The rendered body binds the fragments mapping as a local before
            # `variables` is built and reads the slot runtime module, so a
            # parameter under either name is rebound or shadowed mid-body.
            claims["slot_fragments"].append("the generated slot fragments binding")
            claims["slots"].append("the iron_gql slots module")
        for variable in operation.variables:
            claims[variable.python_name].append(f"variable ${variable.gql_name}")
        for slot in operation.slots:
            claims[slot.python_name].append(f"slot '{slot.name}'")
        at = ", ".join(operation.locations)
        for name, sources in sorted(claims.items()):
            # A parameter name must survive `compile()` of the generated
            # `def execute(...)` — a Python keyword only fails at import.
            if keyword.iskeyword(name):
                message = (
                    f"Execute parameter '{name}' of operation "
                    f"'{operation.class_name}' at {at} is a Python keyword;"
                    " rename the variable or alias the field"
                )
                errors.append(message)
            if len(sources) == 1:
                continue
            claimed = " and by ".join(sources)
            message = (
                f"Execute parameter '{name}' of operation "
                f"'{operation.class_name}' at {at} is claimed by {claimed}"
            )
            errors.append(message)
    return errors
