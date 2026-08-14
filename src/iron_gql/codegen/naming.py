import dataclasses
import heapq
import keyword
from collections import defaultdict
from collections.abc import Iterator
from collections.abc import Mapping
from graphlib import TopologicalSorter
from typing import Literal

from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedBinding
from iron_gql.codegen.ir import CollectedFactoryFragment
from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedFragment
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import CollectedTemplate
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ListRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.ir import field_name_to_pascal
from iron_gql.codegen.ir import slot_param_name
from iron_gql.codegen.render import bind_body_free_names
from iron_gql.codegen.render import bind_signatures
from iron_gql.codegen.render import fragment_init_body_free_names
from iron_gql.codegen.render import operation_execute_free_names
from iron_gql.codegen.render import template_execute_free_names
from iron_gql.codegen.render import with_args_body_free_names


def type_tokens(typ: TypeRef) -> Iterator[str]:
    # The tokens that tell two shapes of one GraphQL type apart. Nullability is
    # one of them: a field a directive can withhold and the same field always
    # present are different shapes of the same name, and leaving the
    # distinction out of the tokens made the two collide under one detailed
    # name rather than being generated as two models.
    if typ.nullable:
        # The trailing underscore is what makes the marker a boundary rather
        # than a prefix that merges into the next token: every token below goes
        # through `field_name_to_pascal`, which strips underscores, so `_` can
        # occur in a token only as a separator this function wrote. Without it,
        # a nullable `Foo` and a non-null type actually named `OptFoo` produce
        # the same string and two different shapes collide under one name.
        yield "Opt_"
    match typ:
        case ListRef(element=element):
            yield "List"
            yield from type_tokens(element)
        case NamedRef(name=name):
            yield field_name_to_pascal(name)
        case ScalarRef(name_hint=hint):
            if hint is not None:
                yield field_name_to_pascal(hint)


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
    # `rendered_type`, not `type_info`: a field a directive can withhold is
    # rendered optional, and that is part of the shape the name has to
    # distinguish. Reading the unconditional type made two models that differ
    # only in what a directive guards collide under one name.
    return "".join(
        token
        for field in sorted(model.fields, key=lambda f: f.name)
        for token in type_tokens(field.rendered_type)
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
    # Fragment models — root и все достижимые из него типы — исключены по другой
    # причине: caller использует их в annotations после `read`, поэтому public
    # names definition не должны зависеть от остальных operations пакета.
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
    "applied_fragment",
    "on_type_base",
    "template",
    "bound_base",
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
    for base in ir.on_type_bases:
        # Base именуется GraphQL type condition, общий для всех фрагментов на
        # этом типе, поэтому у него нет одного source location.
        yield (
            base.name,
            "on_type_base",
            f"the on-type base of '{base.graphql_type_name}'",
        )
    for fragment in ir.fragments:
        origin = f"fragment '{fragment.fragment_name}' at {fragment.location}"
        yield fragment.class_name, "fragment", origin
        if isinstance(fragment, CollectedFactoryFragment):
            yield (
                fragment.applied_class_name,
                "applied_fragment",
                f"the applied class of {origin}",
            )
    for template in ir.templates:
        at = template.location
        yield (
            template.class_name,
            "template",
            f"template '{template.class_name}' at {at}",
        )
        origin = f"the bound base of template '{template.class_name}' at {at}"
        yield template.bound_base_name, "bound_base", origin
    # Binding больше не создаёт собственный class: это entry `_binding_specs`
    # соответствующего template, поэтому module-level Python name здесь нет.


def fixed_module_names(ir: CollectedPackageIR) -> frozenset[str]:
    # IR закрепляет classes operations и fragments за именами, написанными
    # developer, поэтому их нельзя перемещать для разрешения collision. Model
    # перемещать можно. Эти names передаются rename pass как reserved, а ошибками
    # становятся только оставшиеся collisions. Fragment data models и slot
    # models закрепляются отдельно; scaffold names добавляет
    # `render.scaffold_claims`.
    # A template's class name and its `{Name}Bound` base are equally
    # developer-named (the query name, the derived base). An on-type base's
    # name is fixed for a different reason: it names a GraphQL type, not
    # anything a developer wrote in this package, but a fragment's own class
    # derives from it by that exact name, so it cannot move either.
    return frozenset(name for name, _kind, _origin in _fixed_name_claims(ir))


def apply_rename(
    ir: CollectedPackageIR, scaffold_names: frozenset[str]
) -> CollectedPackageIR:
    rename = build_rename_map(
        ir.result_artifacts,
        # Open models закреплены целиком: кроме стабильности public names,
        # open model с `extra="ignore"` не должна объединиться со strict model
        # под общим коротким именем.
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
                    # `validate_collected_names`, so a collision here comes
                    # from the rename map converging two artifacts -- legal
                    # exactly for identical shapes of the same slot.
                    #
                    # Anything else is a *diagnosis*, not an internal
                    # invariant. The detailed name concatenates field names
                    # and type tokens with no separator, and every token has
                    # its underscores stripped, so ordinary schemas collide:
                    # `{a_b, c}` and `{a, b_c}` both spell `ABC`, a list of
                    # `X` spells what a type named `ListX` spells. Reaching
                    # this used to crash as if it were unreachable; a schema
                    # the generator cannot name is the user's to hear about.
                    if (
                        existing.shape_key != renamed_model.shape_key
                        or existing.slot_name != renamed_model.slot_name
                    ):
                        name = renamed_model.name
                        msg = (
                            f"Two different selections derive the same "
                            f"generated name {name!r}; alias one of the "
                            f"fields so the two shapes are named apart"
                        )
                        raise GraphQLGenerationError([msg])
                    continue
                seen_models[renamed_model.name] = renamed_model
                renamed_result_artifacts.append(renamed_model)
            case CollectedUnionAlias():
                renamed_result_artifacts.append(artifact.renamed(rename))
    # Only the result artifacts move: open-model names are pinned out of the
    # rename map, and the per-binding copies of the result models are cut
    # afterwards, from the names this pass settles.
    return dataclasses.replace(ir, result_artifacts=renamed_result_artifacts)


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
    # Classes operations, public fragment definitions, private applied classes,
    # models, templates, их `{Operation}Bound[...]` bases и module scaffold
    # попадают в один namespace. Python оставляет последнее binding, поэтому
    # любое пересечение здесь является ошибкой. Scaffold claims сохраняют origin
    # каждого binding и проверяются тем же правилом.
    claims: dict[str, list[tuple[ClaimKind, str]]] = defaultdict(list)
    for name, kind, origin in _module_name_claims(ir, scaffold):
        claims[name].append((kind, origin))
    errors: list[str] = []
    for name, entries in sorted(claims.items()):
        origins = [origin for _kind, origin in entries]
        if len(entries) > 1:
            message = f"Name '{name}' is claimed by {' and by '.join(origins)}"
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
    for operation in ir.operations:
        at = operation.location
        scope = f"execute() of operation '{operation.class_name}' at {at}"
        yield scope, "self", "the method receiver"
        for name, origin in operation_execute_free_names(operation.result_type):
            yield scope, name, origin
        for variable in operation.variables:
            yield scope, variable.python_name, f"variable ${variable.gql_name}"
    for template in ir.templates:
        yield from _template_signature_claims(template, ir.fragments, ir.bindings)
    # A binding renders no method of its own -- it is a row in its template's
    # `_binding_specs` -- so `ir.bindings` claims no signature namespace here.
    # `with_args` belongs to the fragment whose own variables it
    # applies instead: two of them snaking to the same Python name is a
    # collision within *this* fragment's own closure, unrelated to any other
    # fragment or combination that might also declare a variable.
    for fragment in ir.fragments:
        yield from _fragment_init_claims(fragment)
        yield from _fragment_with_args_claims(fragment)


def _template_signature_claims(
    template: CollectedTemplate,
    fragments: list[CollectedFragment],
    bindings: list[CollectedBinding],
) -> Iterator[tuple[str, str, str]]:
    at = template.location
    scope = f"execute() of template '{template.class_name}' at {at}"
    yield scope, "self", "the method receiver"
    for name, origin in template_execute_free_names(template):
        yield scope, name, origin
    for variable in template.variables:
        yield scope, variable.python_name, f"variable ${variable.gql_name}"
    scope = f"bind() of template '{template.class_name}' at {at}"
    yield scope, "self", "the method receiver"
    # `bind()` carries the template's slots as parameters of the
    # implementation under its stubs, over a body that reads the names
    # below from an enclosing scope. The claim is unconditional, and has
    # to be: `bind()` is rendered either as a set of stubs over an erased
    # implementation or -- when nothing in the package can be spread into
    # any of the template's slots -- as one plain signature, and both
    # write the same parameter names over the same body. Claiming them
    # for only one shape left the other's parameters free to shadow the
    # module its own body calls into.
    for name, origin in bind_body_free_names(template):
        yield scope, name, origin
    for slot in template.slots:
        yield scope, slot.python_name, f"slot '{slot.name}'"
    # The result models' type parameters are a namespace of their own, and
    # a narrower one than `bind()`'s keywords: the phantom name drops the
    # underscores its slot may carry, so `details` and `_details` are two
    # keywords and one parameter. A model would then declare fewer
    # parameters than its bindings pass arguments, and the generated
    # package would fail to import.
    scope = f"the type parameters of template '{template.class_name}' at {at}"
    for slot in template.slots:
        yield scope, slot_param_name(slot.python_name), f"slot '{slot.name}'"
    yield from _bind_signature_type_param_claims(template, fragments, bindings)


def _bind_signature_type_param_claims(
    template: CollectedTemplate,
    fragments: list[CollectedFragment],
    bindings: list[CollectedBinding],
) -> Iterator[tuple[str, str, str]]:
    # PEP 695 даёт каждому rendered overload отдельный namespace type
    # parameters. `render.bind_signatures` — источник точных overloads и имён,
    # которые читают их annotations. Повторное приближённое построение scopes
    # здесь объединило бы раздельные namespaces Python и потеряло бы Cartesian
    # combinations, которые Python видит в одной signature.
    reference_origins = {
        template.bound_base_name: f"the bound base of template '{template.name}'",
        template.result_type: f"the result class of template '{template.name}'",
    }
    for slot in template.slots:
        for base in slot.on_type_bases:
            reference_origins[base] = f"the on-type base '{base}'"
    for name, fragment in _fragments_by_class_name(fragments).items():
        reference_origins[name] = (
            f"fragment '{fragment.fragment_name}' at {fragment.location}"
        )

    for position, signature in enumerate(bind_signatures(template, bindings), 1):
        scope = (
            f"the type parameters of overload {position} of template "
            f"'{template.class_name}' at {template.location}"
        )
        for type_param in signature.type_params:
            yield scope, type_param.name, type_param.origin
        for name in signature.references:
            yield scope, name, reference_origins[name]


def _fragments_by_class_name(
    fragments: list[CollectedFragment],
) -> dict[str, CollectedFragment]:
    # Both spellings a constraint can carry: a plain fragment is named by its
    # own class, a factory by the applied one `with_args` returns.
    by_class: dict[str, CollectedFragment] = {}
    for fragment in fragments:
        by_class[fragment.class_name] = fragment
        if isinstance(fragment, CollectedFactoryFragment):
            by_class[fragment.applied_class_name] = fragment
    return by_class


def _fragment_with_args_claims(
    fragment: CollectedFragment,
) -> Iterator[tuple[str, str, str]]:
    if not isinstance(fragment, CollectedFactoryFragment):
        return
    at = fragment.location
    scope = f"with_args() of fragment '{fragment.fragment_name}' at {at}"
    yield scope, "self", "the method receiver"
    for name, origin in with_args_body_free_names(fragment):
        yield scope, name, origin
    for arg in fragment.arg_vars:
        yield scope, arg.python_name, f"variable ${arg.gql_name}"


def _fragment_init_claims(
    fragment: CollectedFragment,
) -> Iterator[tuple[str, str, str]]:
    at = fragment.location
    scope = f"__init__() of fragment '{fragment.fragment_name}' at {at}"
    yield scope, "self", "the method receiver"
    for name, origin in fragment_init_body_free_names(fragment):
        yield scope, name, origin
    if isinstance(fragment, CollectedFactoryFragment):
        applied_scope = (
            f"__init__() of applied fragment '{fragment.fragment_name}' at {at}"
        )
        yield applied_scope, "self", "the method receiver"
        yield applied_scope, "fragment_args", "the fragment arguments"
        for name, origin in fragment_init_body_free_names(fragment):
            yield applied_scope, name, origin


def validate_signature_names(ir: CollectedPackageIR) -> list[str]:
    # Every generated method takes its parameters in one Python namespace, and
    # names reach it through `to_snake_fn` — so two names that differ in
    # GraphQL can render the same parameter twice, and a name that is not a
    # usable identifier renders a parameter that never compiles.
    #
    # Every parameter namespace the generator writes lives here, not just
    # `execute`'s: a template's slots become `bind()`'s parameters too, and
    # each is as capable of colliding as an operation's variables are.
    claims: dict[tuple[str, str], list[str]] = defaultdict(list)
    for scope, name, origin in _signature_claims(ir):
        claims[scope, name].append(origin)
    for artifact in ir.result_artifacts:
        if not artifact.type_params:
            continue
        # A PEP 695 parameter is local to this artifact. It conflicts only
        # with a module type that this artifact refers to: there the local
        # name changes the annotation's meaning. An input type used solely by
        # execute() lives outside this scope and is therefore unrelated.
        scope = f"generic artifact '{artifact.name}'"
        for type_param in artifact.type_params:
            claims[scope, type_param].append(f"type parameter '{type_param}'")
        for dependency in artifact.dependencies:
            claims[scope, dependency].append(f"referenced type '{dependency}'")
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
