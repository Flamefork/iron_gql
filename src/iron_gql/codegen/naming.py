import heapq
import keyword
from collections import defaultdict
from collections.abc import Iterator
from collections.abc import Mapping
from graphlib import TopologicalSorter
from typing import Literal

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
from iron_gql.codegen.ir import bindings_by_template
from iron_gql.codegen.ir import field_name_to_pascal
from iron_gql.codegen.ir import renders_inline_bind_body
from iron_gql.codegen.render import BIND_BODY_FREE_NAMES


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


def _graphql_type_name(model: CollectedModel) -> str:
    # The one statement of the phases' shared premise: `build_rename_map`
    # selects the models that carry a GraphQL type before any phase below runs,
    # so each of them has one. `CollectedModel.graphql_type_name` is optional
    # because most artifacts have no GraphQL type at all, not because a model
    # the rename pass walks might lack one -- narrowed here, once, instead of
    # at each walk that would otherwise restate the same invariant.
    if model.graphql_type_name is None:
        msg = f"untyped model {model.name!r} reached the rename phases"
        raise AssertionError(msg)
    return model.graphql_type_name


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
        slot_shapes[_graphql_type_name(model), model.field_names_key].add(
            shapes[model.name]
        )

    for model in models:
        gql_type = _graphql_type_name(model)
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
        type_variants[_graphql_type_name(model)].add(rename.get(model.name, model.name))

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


# What kind of thing claims a module-level name. The classification the
# collision hints below branch on: carried beside the message rather than
# recovered from it, so rewording an origin cannot silently switch a hint off.
type ClaimKind = Literal[
    "operation",
    "fragment",
    "singleton",
    "template",
    "bound_base",
    "binding",
    "type_param",
    "scaffold",
    "model",
    "enum",
]


def _fixed_name_claims(ir: CollectedPackageIR) -> Iterator[tuple[str, ClaimKind, str]]:
    # (name, kind, what claims it) for every name the IR pins. The one
    # enumeration of those sources: `fixed_module_names` reserves them for the
    # rename pass and `_module_name_claims` reports collisions between them,
    # and a source listed in only one of the two silently drops the other's
    # protection for every name it contributes.
    for operation in ir.operations:
        at = operation.location
        yield (
            operation.class_name,
            "operation",
            f"operation '{operation.class_name}' at {at}",
        )
    for fragment in ir.fragments:
        origin = f"fragment '{fragment.fragment_name}' at {fragment.location}"
        yield fragment.class_name, "fragment", origin
        yield fragment.singleton_name, "singleton", f"the singleton of {origin}"
    for template in ir.templates:
        at = template.location
        yield (
            template.class_name,
            "template",
            f"template '{template.class_name}' at {at}",
        )
        origin = f"the bound base of template '{template.class_name}' at {at}"
        yield template.bound_base_name, "bound_base", origin
        for slot in template.slots:
            # A PEP 695 type parameter is scoped to the class that declares
            # it, so it shadows a module-level name of the same spelling
            # inside every generic artifact this slot reaches -- silently, and
            # with the wrong type. It pins its name for that reason, even
            # though it never becomes a module-level binding itself.
            origin = (
                f"the type parameter of slot '{slot.name}' in template "
                f"'{template.class_name}' at {at}"
            )
            yield slot.type_param, "type_param", origin
    for binding in ir.bindings:
        yield (
            binding.class_name,
            "binding",
            f"binding '{binding.class_name}' at {binding.location}",
        )


def fixed_module_names(ir: CollectedPackageIR) -> frozenset[str]:
    # The names this IR pins: an operation class and a fragment handle (and
    # that handle's singleton) are each named after something the developer
    # wrote, so none of them can move to resolve a clash. A model can, which
    # is why these are fed to the rename pass as reserved and only the
    # leftovers become errors. Two more sets are pinned elsewhere: fragment
    # data models via the rename pass's pinned names, and slot models by
    # their exclusion from it. The module also binds the scaffold's own
    # names — the client, the base models, the dispatch dicts and every
    # import — which the IR knows nothing about; `render.scaffold_claims`
    # supplies those and both sets are used together everywhere.
    # A template's class name and its `{Name}Bound` base are equally
    # developer-named (the query name, the derived base), and a binding's
    # class name is derived from those same names plus its fragments' -- all
    # equally fixed.
    return frozenset(name for name, _kind, _origin in _fixed_name_claims(ir))


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
                        or existing.slot_params != renamed_model.slot_params
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
        templates=ir.templates,
        bindings=ir.bindings,
        enums=ir.enums,
        # Open-model names pass through untouched: every one of them is pinned
        # out of the rename map.
        open_model_names=ir.open_model_names,
        discovered_texts=ir.discovered_texts,
    )


def _is_usable_identifier(name: str) -> bool:
    # Whether the generated module survives `compile()` with this name written
    # into it. Nothing downstream of codegen compiles the module, so a keyword
    # or a non-identifier surfaces as a SyntaxError inside the user's own
    # import unless it is caught here.
    return name.isidentifier() and not keyword.iskeyword(name)


def _module_name_claims(
    ir: CollectedPackageIR, scaffold: Mapping[str, tuple[str, ...]]
) -> Iterator[tuple[str, ClaimKind, str]]:
    for name in sorted(scaffold):
        for origin in scaffold[name]:
            yield name, "scaffold", origin
    yield from _fixed_name_claims(ir)
    for artifact in (*ir.result_artifacts, *ir.input_artifacts):
        yield artifact.name, "model", f"model '{artifact.name}'"
    for enum in ir.enums:
        yield enum.name, "enum", f"enum '{enum.name}'"


def validate_module_names(
    ir: CollectedPackageIR, scaffold: Mapping[str, tuple[str, ...]]
) -> list[str]:
    # Operation classes, fragment handles, their singletons, models, template
    # classes and their `{Operation}Bound[...]` bases, bindings, and the
    # scaffold the module is built on (client, base models, dispatch dicts,
    # imports) all land in one namespace, and
    # Python binds the last one written. Every rebinding here breaks something
    # silently — dispatch resolving to a handle, a base class replaced by a
    # model, `API_CLIENT` replaced by a fragment singleton — so any overlap is
    # an error. Scaffold claims carry the origin of each binding, so two
    # scaffold sources fighting over one name are an overlap like any other.
    claims: dict[str, list[tuple[ClaimKind, str]]] = defaultdict(list)
    for name, kind, origin in _module_name_claims(ir, scaffold):
        claims[name].append((kind, origin))
    errors: list[str] = []
    for name, entries in sorted(claims.items()):
        origins = [origin for _kind, origin in entries]
        kinds = {kind for kind, _origin in entries}
        # Two templates naming a slot alike is not a clash: each type
        # parameter is scoped to its own class, so they never share a
        # namespace with each other -- only with the module-level names below.
        if len(entries) > 1 and kinds != {"type_param"}:
            message = f"Name '{name}' is claimed by {' and by '.join(origins)}"
            if "singleton" in kinds:
                message += (
                    "; rename the fragment so its class and singleton names differ"
                )
            elif kinds == {"binding"}:
                # Two combinations whose slot and fragment names split the
                # same letters two ways. Neither call site named the class --
                # the combination did -- so the fix is on the names it is
                # built from: the slot's and the fragments'.
                message += "; alias the slot field or rename one of the fragments"
            errors.append(message)
        # Every claim becomes a module-level binding of its own.
        if not _is_usable_identifier(name):
            errors.append(
                f"Name '{name}' ({origins[0]}) is not a usable Python identifier"
            )
    return errors


def _signature_claims(ir: CollectedPackageIR) -> Iterator[tuple[str, str, str]]:
    # (scope, parameter name, what claims it) for every method the generator
    # writes parameters onto. Each scope is one Python keyword namespace: the
    # method's own parameters, its receiver, and any name its rendered body
    # binds or reads from an enclosing scope. A name is claimed only where the
    # renderer really writes it: a claim the generated body never makes turns
    # a legal GraphQL name into a generation error with no way out.
    grouped = bindings_by_template(ir.bindings)
    for operation in ir.operations:
        at = operation.location
        scope = f"execute() of operation '{operation.class_name}' at {at}"
        yield scope, "self", "the method receiver"
        for variable in operation.variables:
            yield scope, variable.python_name, f"variable ${variable.gql_name}"
    for template in ir.templates:
        at = template.location
        scope = f"execute() of template '{template.class_name}' at {at}"
        yield scope, "self", "the method receiver"
        for variable in template.variables:
            yield scope, variable.python_name, f"variable ${variable.gql_name}"
        scope = f"bind() of template '{template.class_name}' at {at}"
        yield scope, "self", "the method receiver"
        if renders_inline_bind_body(grouped.get(template.class_name, [])):
            # Only that form's `bind()` has a real body, so only it reads
            # names from outside its own parameters -- and which ones is the
            # renderer's to say. Otherwise the slots are parameters of
            # `@overload` stubs whose body is `...`, over an implementation
            # whose only parameter is `**fragments`, so nothing can be
            # shadowed there.
            for name, origin in BIND_BODY_FREE_NAMES:
                yield scope, name, origin
        for slot in template.slots:
            yield scope, slot.python_name, f"slot '{slot.name}'"
    for binding in ir.bindings:
        # `render._render_with_args` builds the variables mapping as a single
        # expression, so `with_args()`'s namespace holds nothing but its
        # receiver and its parameters.
        scope = f"with_args() of binding '{binding.class_name}' at {binding.location}"
        yield scope, "self", "the method receiver"
        for arg in binding.arg_vars:
            yield (
                scope,
                arg.var.python_name,
                f"fragment variable ${arg.var.gql_name}",
            )


def validate_signature_names(ir: CollectedPackageIR) -> list[str]:
    # Every generated method takes its parameters in one Python namespace, and
    # names reach it through `to_snake_fn` — so two names that differ in
    # GraphQL can render the same parameter twice, and a name that is not a
    # usable identifier renders a parameter that never compiles.
    #
    # Every parameter namespace the generator writes lives here, not just
    # `execute`'s: a template's slots become `bind()`'s parameters and a
    # binding's fragment variables become `with_args()`'s, and each of those
    # is as capable of colliding as an operation's variables are.
    claims: dict[tuple[str, str], list[str]] = defaultdict(list)
    for scope, name, origin in _signature_claims(ir):
        claims[scope, name].append(origin)
    errors: list[str] = []
    for (scope, name), origins in sorted(claims.items()):
        if not _is_usable_identifier(name):
            unusable = "is not a usable Python identifier"
            hint = "rename the variable or alias the field"
            errors.append(f"Parameter '{name}' of {scope} {unusable}; {hint}")
        if len(origins) > 1:
            claimed = " and by ".join(origins)
            errors.append(f"Parameter '{name}' of {scope} is claimed by {claimed}")
    return errors
