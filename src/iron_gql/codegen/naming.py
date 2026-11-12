import ast
import dataclasses
import hashlib
import heapq
import json
import keyword
from collections import defaultdict
from collections.abc import Iterator
from collections.abc import Mapping
from graphlib import TopologicalSorter
from typing import Literal

from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedBinding
from iron_gql.codegen.ir import CollectedFactoryFragment
from iron_gql.codegen.ir import CollectedFragment
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedModuleIR
from iron_gql.codegen.ir import CollectedTemplate
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ListRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.ir import field_name_to_pascal
from iron_gql.codegen.ir import slot_param_name
from iron_gql.codegen.ir import slot_roots
from iron_gql.codegen.render import bind_body_free_names
from iron_gql.codegen.render import bind_signatures
from iron_gql.codegen.render import fragment_init_body_free_names
from iron_gql.codegen.render import operation_execute_free_names
from iron_gql.codegen.render import render_artifacts
from iron_gql.codegen.render import template_execute_free_names
from iron_gql.codegen.render import with_args_body_free_names
from iron_gql.codegen.slots import reachable_model_names


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


def _graphql_type_name(artifact: CollectedArtifact) -> str:
    # The one statement of the phases' shared premise: `build_rename_map`
    # selects the artifacts that carry a GraphQL type before any phase below
    # runs, so each of them has one. `CollectedModel.graphql_type_name` is
    # optional because a result root has no GraphQL type to be named after,
    # not because a model the rename pass walks might lack one -- narrowed
    # here, once, instead of at each walk that would otherwise restate the
    # same invariant.
    if artifact.graphql_type_name is None:
        msg = f"untyped model {artifact.name!r} reached the rename phases"
        raise AssertionError(msg)
    return artifact.graphql_type_name


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


def topological_sort[A: CollectedArtifact](artifacts: list[A]) -> list[A]:
    if not artifacts:
        return artifacts
    by_name = {artifact.name: artifact for artifact in artifacts}
    sorter = TopologicalSorter({
        artifact.name: {dep for dep in artifact.dependencies if dep in by_name}
        for artifact in artifacts
    })
    sorter.prepare()

    # Heap breaks ties between simultaneously-ready nodes by name for determinism;
    # TopologicalSorter.get_ready() makes no ordering guarantee on its own.
    queue = list(sorter.get_ready())
    heapq.heapify(queue)
    result: list[A] = []
    while queue:
        name = heapq.heappop(queue)
        result.append(by_name[name])
        sorter.done(name)
        for ready_name in sorter.get_ready():
            heapq.heappush(queue, ready_name)

    return result


def _field_suffix(field_names: tuple[str, ...]) -> str:
    return "".join(field_name_to_pascal(name) for name in field_names)


# The identity of an artifact's shape, independent of any name this pass
# chooses: the GraphQL type and the full content of every field or variant,
# with each reference to another renamable artifact replaced by that
# artifact's own key. Names are a lossy digest of a shape -- tokens
# concatenate without separators and lose underscores -- so deciding "same
# shape" by name merged different shapes or failed generation; the phases
# below decide it by this key instead (ADR 0001).
type KeyPart = str | bool | tuple[KeyPart, ...] | None
type ShapeKey = tuple[KeyPart, ...]


def _type_key(typ: TypeRef, keys: Mapping[str, ShapeKey]) -> KeyPart:
    match typ:
        case ScalarRef(expr=expr, name_hint=hint):
            body: KeyPart = ("scalar", expr, hint)
        case NamedRef(name=name, params=params):
            # An artifact this pass renames is referred to by its shape, since
            # its name is not settled yet; anything else keeps a fixed name,
            # and that name is what the rendered field will say.
            target = keys.get(name, name)
            body = ("ref", target, params)
        case ListRef(element=element):
            body = ("list", _type_key(element, keys))
    return (body, typ.nullable)


def _shape_key(artifact: CollectedArtifact, keys: Mapping[str, ShapeKey]) -> ShapeKey:
    match artifact:
        case CollectedModel():
            return (
                _graphql_type_name(artifact),
                artifact.type_params,
                tuple(
                    (
                        field.name,
                        field.response_key,
                        _type_key(field.type_info, keys),
                        field.default_expr,
                        field.is_conditional,
                    )
                    for field in artifact.shape_key
                ),
            )
        case CollectedUnionAlias():
            # Five parts against a model's three, so no alias key equals a
            # model key whatever its GraphQL type is called. A slot's alias
            # stays apart from every other alias through its variants: they
            # are slot models, which never move, so the key names them.
            return (
                "union",
                artifact.graphql_type_name,
                artifact.type_params,
                tuple(_type_key(variant, keys) for variant in artifact.variants),
                artifact.discriminator,
            )


def _shape_digest(key: ShapeKey) -> str:
    # A stable serialization, not `hash()` (salted per process) nor a `repr`
    # of objects: the suffix is part of a public class name and must be the
    # same on every run and every machine.
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


# The digest prefixes a suffixed name tries, shortest first. A suffix that is
# still contested moves to a longer prefix of the same digest rather than to
# an ordinal, so it stays a function of the shape it names.
_DIGEST_LENGTHS = (8, 16, 32, 64)


def _shape_keys(artifacts: list[CollectedArtifact]) -> dict[str, ShapeKey]:
    # Topological order: an artifact's key embeds the keys of its dependencies,
    # across both kinds -- a model's field names an alias, an alias's variant
    # names a model.
    keys: dict[str, ShapeKey] = {}
    for artifact in artifacts:
        keys[artifact.name] = _shape_key(artifact, keys)
    return keys


def _short_name(model: CollectedModel) -> str:
    return f"{_graphql_type_name(model)}With{_field_suffix(model.field_names_key)}"


def _detailed_candidate(model: CollectedModel, spellings: dict[str, str]) -> str:
    # The detailed name of a model's shape. A nested artifact is spelled one
    # level deep (ADR 0003): a model by its `{Type}With{Fields}`, an alias by
    # its abstract type -- never by its own detailed name, which made the name
    # grow with the depth of the selection. The candidate is then a function
    # of the shape alone, and two shapes it no longer tells apart are told
    # apart by their digest suffixes.
    spelled = model.renamed(spellings)
    return f"{_short_name(model)}_{_model_type_name_tokens(spelled)}"


def _one_level_spellings(artifacts: list[CollectedArtifact]) -> dict[str, str]:
    # Over every artifact, not only the ones this pass renames: a slot model or
    # a pinned fragment model is spelled one level deep too, rather than by its
    # path name, which carries the operation's name and an anonymous one's
    # hash into every parent that refers to it.
    spellings: dict[str, str] = {}
    for artifact in artifacts:
        match artifact:
            case CollectedModel(graphql_type_name=str(gql_type)):
                fields = _field_suffix(artifact.field_names_key)
                spellings[artifact.name] = f"{gql_type}With{fields}"
            case CollectedModel():
                # A result root: named after its operation, and no field
                # refers to it.
                pass
            case CollectedUnionAlias():
                spellings[artifact.name] = artifact.graphql_type_name
    return spellings


# Who stands behind a name: the GraphQL type behind a bare name, the
# short-name claimant (type and field names) behind a short one, the shape
# behind a detailed or suffixed one. Every claimant claims its name whether or
# not it can use it -- a type with two shapes still claims its bare name -- so
# the set of claimants of a name only grows as queries are added.
type Claimant = tuple[str, KeyPart]


@dataclasses.dataclass(frozen=True, kw_only=True)
class _Option:
    name: str
    claimant: Claimant
    # Whether the claimant has one shape to give the name to: always for a
    # shape, for a type or short-name claimant only when exactly one shape lives
    # in it.
    usable: bool


def _options(
    artifact: CollectedArtifact,
    key: ShapeKey,
    spellings: dict[str, str],
    shapes_in: Mapping[Claimant, set[ShapeKey]],
) -> list[_Option]:
    # The names a shape may take, most preferred first. Models and aliases of
    # one GraphQL type claim its bare name together (ADR 0003): an interface's
    # fallback model and an alias over that interface are two shapes of it,
    # and neither may hold the name while the other exists.
    gql_type = _graphql_type_name(artifact)
    of_type: Claimant = ("type", gql_type)
    of_shape: Claimant = ("shape", key)
    digest = _shape_digest(key)
    bare = _Option(name=gql_type, claimant=of_type, usable=len(shapes_in[of_type]) == 1)
    match artifact:
        case CollectedModel():
            of_fields: Claimant = ("short_name", (gql_type, artifact.field_names_key))
            candidate = _detailed_candidate(artifact, spellings)
            return [
                bare,
                _Option(
                    name=_short_name(artifact),
                    claimant=of_fields,
                    usable=len(shapes_in[of_fields]) == 1,
                ),
                _Option(name=candidate, claimant=of_shape, usable=True),
                *(
                    _Option(
                        name=f"{candidate}_{digest[:length]}",
                        claimant=of_shape,
                        usable=True,
                    )
                    for length in _DIGEST_LENGTHS
                ),
            ]
        case CollectedUnionAlias():
            # No name by path: the path of an anonymous operation carries its
            # hash, and a path name would have to be claimed for good or pass
            # to another shape when it frees up (ADR 0003).
            return [
                bare,
                *(
                    _Option(
                        name=f"{gql_type}_{digest[:length]}",
                        claimant=of_shape,
                        usable=True,
                    )
                    for length in _DIGEST_LENGTHS
                ),
            ]


def _choose_names(
    artifacts: list[CollectedArtifact],
    keys: Mapping[str, ShapeKey],
    spellings: dict[str, str],
    occupied: frozenset[str],
    fixed: Mapping[str, str],
) -> dict[ShapeKey, str]:
    # ADR 0001: a name goes to its sole claimant or to nobody. A held name can
    # then only disappear as queries are added -- its claimants grow, or its
    # type or short-name claimant gains a second shape -- and never pass to
    # another shape, which would have had to claim it all along.
    shapes_in: dict[Claimant, set[ShapeKey]] = defaultdict(set)
    for artifact in artifacts:
        key = keys[artifact.name]
        gql_type = _graphql_type_name(artifact)
        shapes_in["type", gql_type].add(key)
        if isinstance(artifact, CollectedModel):
            shapes_in["short_name", (gql_type, artifact.field_names_key)].add(key)
    options = {
        keys[artifact.name]: _options(
            artifact, keys[artifact.name], spellings, shapes_in
        )
        for artifact in artifacts
    }
    _reject_fixed_claims(options, fixed)
    claimants: dict[str, set[Claimant]] = defaultdict(set)
    for shape_options in options.values():
        for option in shape_options:
            claimants[option.name].add(option.claimant)
    names: dict[ShapeKey, str] = {}
    exhausted: list[str] = []
    for key, shape_options in options.items():
        free = [
            option.name
            for option in shape_options
            if option.usable
            and option.name not in occupied
            and claimants[option.name] == {option.claimant}
        ]
        if free:
            names[key] = free[0]
        else:
            exhausted.append(_exhausted_message(shape_options, occupied))
    if exhausted:
        # Reached only when operations, fragments or types are spelled after
        # the digests of a shape. Counting past them would hand the count to
        # whatever is spelled like it later -- a silent move -- so the user
        # hears about it instead.
        raise GraphQLGenerationError(sorted(exhausted))
    return names


def _reject_fixed_claims(
    options: Mapping[ShapeKey, list[_Option]], fixed: Mapping[str, str]
) -> None:
    # ADR 0004: a class named by the developer never moves, so a shape that
    # merely skipped its name would hold another one -- and take this one the
    # day the operation is renamed, or lose it the day the operation is
    # added, under the code that already imports it. Every name a shape
    # claims counts, chosen or not: which one it ends up with depends on the
    # rest of the module, and the error must not.
    errors = {
        (
            f"Name '{option.name}' of {fixed[option.name]} is also claimed by a "
            f"selection of GraphQL type '{gql_type}' (its own name "
            f"'{_own_name(shape_options)}'); rename the operation, template "
            "or fragment so the two cannot meet"
        )
        for shape_options in options.values()
        # The bare name leads every shape's options, and it is the type.
        for gql_type in [shape_options[0].name]
        for option in shape_options
        if option.name in fixed
    }
    if errors:
        raise GraphQLGenerationError(sorted(errors))


def _own_names(shape_options: list[_Option]) -> list[str]:
    # The names the shape alone may hold: its detailed name and its suffixes.
    return [option.name for option in shape_options if option.claimant[0] == "shape"]


def _own_name(shape_options: list[_Option]) -> str:
    return _own_names(shape_options)[0]


def _exhausted_message(shape_options: list[_Option], occupied: frozenset[str]) -> str:
    own = _own_names(shape_options)
    blocked = ", ".join(
        f"'{name}' ({'taken' if name in occupied else 'also spelled elsewhere'})"
        for name in own
    )
    return (
        f"No name is left for the selection named '{own[0]}': {blocked}; "
        "rename the operation, fragment or GraphQL type that holds one of them"
    )


def _is_renamable(artifact: CollectedArtifact, fixed: Mapping[str, str]) -> bool:
    # Every artifact named after something the developer wrote stays put:
    # result roots, named after their operation, and the slot and fragment
    # subtrees (`_developer_named`). Slot models because the collapse phases
    # merge models that share a GraphQL type and field set, and two slots with
    # the same static selection (`{ __typename }` alone is the common case)
    # would end up as one class whose single `slot_name__` could only name one
    # of them; fragment artifacts because a caller writes them in annotations
    # after `read`, so their names must not depend on the other operations of
    # the module. The alias over a slot's variants is not a slot model and
    # moves; its key names the slot models, so two slots' aliases never merge.
    return artifact.name not in fixed


def build_rename_map(
    artifacts: list[CollectedArtifact],
    reserved_names: frozenset[str],
    fixed: Mapping[str, str],
) -> dict[str, str]:
    # `reserved_names` are taken by something that is not a class -- the
    # scaffold, or a field whose default binds its name where annotations are
    # read -- so a shape skips them. `fixed` maps each class the developer
    # named to what it is: a shape that claims one of those fails generation
    # instead (`_reject_fixed_claims`).
    renamable = topological_sort([
        artifact for artifact in artifacts if _is_renamable(artifact, fixed)
    ])
    keys = _shape_keys(renamable)
    spellings = _one_level_spellings(artifacts)
    names = _choose_names(renamable, keys, spellings, reserved_names, fixed)
    return {
        artifact.name: names[keys[artifact.name]]
        for artifact in renamable
        if names[keys[artifact.name]] != artifact.name
    }


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


def _fixed_name_claims(ir: CollectedModuleIR) -> Iterator[tuple[str, ClaimKind, str]]:
    # (name, kind, what claims it) for every name the IR pins. The one
    # enumeration of those sources: `_developer_named` guards them against
    # the rename pass and `_module_name_claims` reports collisions between them,
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
        # A base is named by the GraphQL type condition shared by all fragments
        # on that type, so it has no single source location.
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
    # A binding no longer creates a class of its own: it is an entry in the
    # matching template's `_binding_specs`, so there is no module-level Python
    # name here.


def _developer_named(ir: CollectedModuleIR) -> dict[str, str]:
    # Every class the IR names after something the developer wrote, mapped to
    # what it is: the IR's fixed claims, the result roots, named after their
    # operation, and every artifact of a slot or fragment subtree, named along
    # the path from its template or fragment. Those are artifacts rather than
    # claims of their own (`_module_name_claims` lists them as models), but a
    # shape that merely skipped one of their names would take it over the day
    # the fragment or template is renamed, under code that already imports it
    # (ADR 0001). An on-type base names a GraphQL type rather than anything
    # written for this module, but a fragment's own class derives from it by
    # that exact name, so it cannot move either.
    named = {name: origin for name, _kind, origin in _fixed_name_claims(ir)}
    for operation in ir.operations:
        named[operation.result_type] = (
            f"the result class of operation '{operation.class_name}' "
            f"at {operation.location}"
        )
    for template in ir.templates:
        named[template.result_type] = (
            f"the result class of template '{template.class_name}' "
            f"at {template.location}"
        )
    by_name = {artifact.name: artifact for artifact in ir.result_artifacts}
    subtrees = [
        *(
            (
                fragment.model_name,
                f"fragment '{fragment.fragment_name}' at {fragment.location}",
            )
            for fragment in ir.fragments
        ),
        *(
            (model.name, f"slot '{slot}'")
            for model, slot in slot_roots(by_name.values())
        ),
    ]
    for root, owner in subtrees:
        named.setdefault(root, f"the model of {owner}")
        for name in sorted(reachable_model_names([root], by_name)):
            named.setdefault(name, f"a model inside {owner}")
    return named


def apply_rename(
    ir: CollectedModuleIR, scaffold_names: frozenset[str]
) -> CollectedModuleIR:
    rename = build_rename_map(
        ir.result_artifacts,
        scaffold_names | _defaulted_field_names(ir.result_artifacts),
        _developer_named(ir),
    )
    renamed_result_artifacts: list[CollectedArtifact] = []
    seen: dict[str, CollectedArtifact] = {}
    for artifact in ir.result_artifacts:
        renamed = artifact.renamed(rename)
        existing = seen.get(renamed.name)
        if existing is None:
            seen[renamed.name] = renamed
            renamed_result_artifacts.append(renamed)
            continue
        # Raw-name twins are rejected upfront by `validate_collected_names`,
        # and the rename map gives one name to exactly one shape key, so two
        # artifacts converge here only as identical shapes: two models of one
        # slot, or two aliases over the same variants.
        if not _same_shape(existing, renamed):
            msg = f"two shapes renamed to {renamed.name!r}"
            raise AssertionError(msg)
    # Only the result artifacts move: open-model names are pinned out of the
    # rename map, and the per-binding copies of the result models are cut
    # afterwards, from the names this pass settles.
    return dataclasses.replace(ir, result_artifacts=renamed_result_artifacts)


def _defaulted_field_names(artifacts: list[CollectedArtifact]) -> frozenset[str]:
    # A field with a default binds its name in the class body, and pydantic
    # resolves the class's annotations there before the module: a model named
    # like such a field would be read as the default (`item: item | None =
    # None` reads `None`) and the module would fail to import. Reserved across
    # the module rather than per referring class: lowercase type names are
    # rare enough that the extra precision would buy nothing.
    return frozenset(
        field.name
        for artifact in artifacts
        if isinstance(artifact, CollectedModel)
        for field in artifact.fields
        if field.rendered_default is not None
    )


def _same_shape(first: CollectedArtifact, second: CollectedArtifact) -> bool:
    match first, second:
        case CollectedModel(), CollectedModel():
            return (
                first.shape_key == second.shape_key
                and first.slot_name == second.slot_name
            )
        case _:
            return first == second


def _is_usable_identifier(name: str) -> bool:
    # Whether the generated module survives `compile()` with this name written
    # into it. Nothing downstream of codegen compiles the module, so a keyword
    # or a non-identifier surfaces as a SyntaxError inside the user's own
    # import unless it is caught here.
    return name.isidentifier() and not keyword.iskeyword(name)


def _module_name_claims(
    ir: CollectedModuleIR, scaffold: Mapping[str, tuple[str, ...]]
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
    ir: CollectedModuleIR, scaffold: Mapping[str, tuple[str, ...]]
) -> list[str]:
    # Operation classes, public fragment definitions, private applied classes,
    # models, templates, their `{Operation}Bound[...]` bases and the module
    # scaffold land in one namespace. Python keeps the last binding, so any
    # overlap here is an error. Scaffold claims keep the origin of each binding
    # and are checked by the same rule.
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


def validate_class_scopes(ir: CollectedModuleIR) -> list[str]:
    # A field with a default binds its name in the class body, and pydantic
    # resolves the class's annotations in that body before the module's: a
    # field named like anything they read -- a scalar's module, a builtin, an
    # enum, a model that does not move -- fails the module at import
    # (`datetime: datetime.datetime | None = None` reads `None.datetime`).
    # Read off the rendered classes, so no annotation the renderer writes
    # escapes the check. A model or alias that moves skips these names instead
    # (`_defaulted_field_names`).
    sources = [
        *render_artifacts(ir.result_artifacts, ir.open_model_names),
        *render_artifacts(ir.input_artifacts, frozenset()),
    ]
    errors: list[str] = []
    for source in sources:
        for node in ast.parse(source).body:
            if not isinstance(node, ast.ClassDef):
                continue
            fields = [stmt for stmt in node.body if isinstance(stmt, ast.AnnAssign)]
            # Pydantic puts the class itself ahead of its own attributes, so
            # a recursive input's `foo: foo | None = None` reads the class.
            read = {
                name.id
                for stmt in fields
                for name in ast.walk(stmt.annotation)
                if isinstance(name, ast.Name)
            } - {node.name}
            errors.extend(
                (
                    f"Field '{stmt.target.id}' of model '{node.name}' has a "
                    f"default while the class's annotations read "
                    f"'{stmt.target.id}', so pydantic would resolve them to "
                    "that default; choose a different alias or schema name"
                )
                for stmt in fields
                if stmt.value is not None
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id in read
            )
    return errors


def _signature_claims(ir: CollectedModuleIR) -> Iterator[tuple[str, str, str]]:
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
    # implementation or -- when nothing in the module can be spread into
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
    # module would fail to import.
    scope = f"the type parameters of template '{template.class_name}' at {at}"
    for slot in template.slots:
        yield scope, slot_param_name(slot.python_name), f"slot '{slot.name}'"
    yield from _bind_signature_type_param_claims(template, fragments, bindings)


def _bind_signature_type_param_claims(
    template: CollectedTemplate,
    fragments: list[CollectedFragment],
    bindings: list[CollectedBinding],
) -> Iterator[tuple[str, str, str]]:
    # PEP 695 gives each rendered overload its own type parameter namespace.
    # `render.bind_signatures` is the source of the exact overloads and the
    # names their annotations read. Rebuilding the scopes approximately here
    # would merge Python's separate namespaces and lose the Cartesian
    # combinations Python sees in one signature.
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


def validate_signature_names(ir: CollectedModuleIR) -> list[str]:
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
