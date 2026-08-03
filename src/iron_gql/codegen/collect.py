from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import Protocol
from typing import cast
from warnings import warn

import graphql
from graphql.execution.execute import get_field_def

from iron_gql.codegen.accessors import field_type
from iron_gql.codegen.accessors import union_types
from iron_gql.codegen.accessors import wrapping_of_type
from iron_gql.codegen.collect_inputs import collect_input_artifacts
from iron_gql.codegen.collect_inputs import collect_input_type_closure
from iron_gql.codegen.ir import BUILTIN_SCALARS
from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedEnum
from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedFragment
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedOperation
from iron_gql.codegen.ir import CollectedOperationVar
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import CollectedSlot
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ImportRef
from iron_gql.codegen.ir import ListRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import StrTransform
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.ir import make_optional
from iron_gql.codegen.ir import slot_roots
from iron_gql.codegen.names import validate_collected_names
from iron_gql.codegen.parser import FragmentStatement
from iron_gql.codegen.parser import Query
from iron_gql.codegen.parser import Statement
from iron_gql.codegen.selection import ALWAYS
from iron_gql.codegen.selection import ConditionalNode
from iron_gql.codegen.selection import SelectionRoots
from iron_gql.codegen.selection import collect_conditional_fields
from iron_gql.codegen.selection import interface_has_base_typename
from iron_gql.codegen.selection import resolve_explicit_types
from iron_gql.codegen.selection import uncovered_assignment
from iron_gql.codegen.slots import fragment_base_name
from iron_gql.codegen.slots import has_slot_directive
from iron_gql.codegen.slots import reachable_model_names
from iron_gql.codegen.slots import spreads_into
from iron_gql.codegen.util import capitalize_first
from iron_gql.codegen.warnings import GraphQLDeprecationWarning
from iron_gql.codegen.warnings import UnknownGQLTypeWarning
from iron_gql.codegen.warnings import warn_deprecated_field


@dataclass(kw_only=True, frozen=True)
class CollectionContext:
    query_name: str
    location: str
    fragments: dict[str, graphql.FragmentDefinitionNode]


def fragment_model_name(fragment_name: str) -> str:
    # Named after the fragment and kept out of the rename pass: a caller writes
    # this type in helper signatures, so it must not shift when unrelated
    # operations join the package.
    return f"{capitalize_first(fragment_name)}Data"


def python_field_name(
    response_key: str,
    to_snake_fn: StrTransform,
) -> str:
    if response_key.startswith("__"):
        return to_snake_fn(f"{response_key[2:]}__")
    return to_snake_fn(response_key)


def _reject_conditionally_merged_slot(
    *,
    response_key: str,
    entries: list[ConditionalNode],
    query_name: str,
    location: str,
) -> None:
    # The invariant: the slot's fragment spreads are spliced into the sent
    # query exactly where a @slot node selects the key, so no variable
    # assignment may keep the key in the response while excluding every @slot
    # node — the key would arrive without the fragments' fields. The slot
    # field itself cannot carry @skip/@include, so its condition here is
    # purely inherited from conditional parents the node is nested under;
    # the check runs over the exact condition of every node merged into the
    # key.
    slot_conds = [entry.cond for entry in entries if has_slot_directive(entry.node)]
    if not slot_conds:
        return
    plain_conds = [
        entry.cond for entry in entries if not has_slot_directive(entry.node)
    ]
    witness = uncovered_assignment(plain_conds, slot_conds)
    if witness is None:
        return
    at = ", ".join(
        f"${name}={'true' if value else 'false'}"
        for name, value in sorted(witness.items())
    )
    msg = (
        f"Slot '{response_key}' in '{query_name}' at {location} is conditional "
        f"while response key '{response_key}' is merged from selections "
        f"without @slot: at {at} the server returns the key without the "
        "slot's fragments. Merge the selections or move the slot out of the "
        "conditional branch"
    )
    raise GraphQLGenerationError([msg])


@dataclass(kw_only=True)
class PackageCollector:
    scalars: dict[str, ImportRef]
    to_snake_fn: StrTransform
    schema: graphql.GraphQLSchema
    enums: dict[str, CollectedEnum] = field(default_factory=dict)

    def collect_type(
        self,
        gql_type: graphql.GraphQLType,
        *,
        nullable: bool = True,
        child_model_name: str | None = None,
    ) -> TypeRef:
        match gql_type:
            case graphql.GraphQLNonNull():
                return self.collect_type(
                    wrapping_of_type(gql_type),
                    nullable=False,
                    child_model_name=child_model_name,
                )
            case graphql.GraphQLList():
                typ: TypeRef = ListRef(
                    element=self.collect_type(
                        wrapping_of_type(gql_type), child_model_name=child_model_name
                    )
                )
            case graphql.GraphQLNamedType() if child_model_name is not None:
                typ = NamedRef(name=child_model_name)
            case graphql.GraphQLNamedType():
                typ = self.field_type_ref(gql_type)
            case _:
                msg = f"Unknown GraphQL type: {gql_type}"
                raise TypeError(msg)
        if nullable:
            return make_optional(typ)
        return typ

    def field_type_ref(
        self,
        gql_type: graphql.GraphQLNamedType,
    ) -> TypeRef:
        match gql_type:
            case graphql.GraphQLScalarType(name=name):
                if name in self.scalars:
                    return ScalarRef(expr=self.scalars[name].dotted_path)
                if name in BUILTIN_SCALARS:
                    return ScalarRef(expr=BUILTIN_SCALARS[name])
                warn(
                    f"Unknown scalar type: {name}, mapped to 'object'",
                    category=UnknownGQLTypeWarning,
                    stacklevel=1,
                )
                return ScalarRef(expr="object", name_hint="Object")
            case graphql.GraphQLInputObjectType(name=name):
                return NamedRef(name=name)
            case graphql.GraphQLEnumType():
                self._collect_enum(gql_type)
                return NamedRef(name=gql_type.name)
            case _:
                type_desc = f"{gql_type.name} ({type(gql_type).__name__})"
                warn(
                    f"Unknown GraphQL type: {type_desc}, mapped to 'object'",
                    category=UnknownGQLTypeWarning,
                    stacklevel=1,
                )
                return ScalarRef(expr="object", name_hint="Object")

    def _collect_enum(
        self,
        gql_type: graphql.GraphQLEnumType,
    ) -> None:
        if gql_type.name in self.enums:
            return
        for value_name, enum_value in gql_type.values.items():
            if enum_value.deprecation_reason is not None:
                value_path = f"{gql_type.name}.{value_name}"
                reason = enum_value.deprecation_reason
                warn(
                    f"Enum value '{value_path}' is deprecated: {reason}",
                    GraphQLDeprecationWarning,
                    stacklevel=2,
                )
        self.enums[gql_type.name] = CollectedEnum(
            name=gql_type.name,
            values=tuple(gql_type.values),
        )

    def collect_operation_models(
        self,
        query: Query,
    ) -> list[CollectedArtifact]:
        ctx = CollectionContext(
            query_name=query.name,
            location=query.stmt.location,
            fragments=query.fragments,
        )
        return self._collect_object_model(
            model_name_base=f"{capitalize_first(query.name)}Result",
            runtime_type=query.root_type,
            roots=((query.operation_def.selection_set, ALWAYS),),
            ctx=ctx,
        )

    def collect_fragment_models(
        self,
        statement: FragmentStatement,
    ) -> list[CollectedArtifact]:
        ctx = CollectionContext(
            query_name=statement.name,
            location=statement.stmt.location,
            fragments=statement.fragments,
        )
        return self._collect_composite_model(
            base_name=fragment_model_name(statement.name),
            named=self.schema.get_type(statement.type_condition),
            roots=((statement.definition.selection_set, ALWAYS),),
            ctx=ctx,
            origin=f"fragment {statement.name}",
        )

    def _collect_object_model(
        self,
        *,
        model_name_base: str,
        runtime_type: graphql.GraphQLObjectType,
        roots: SelectionRoots,
        ctx: CollectionContext,
        typename_type: TypeRef | None = None,
        require_typename_for: str | None = None,
        graphql_type_name: str | None = None,
        slot_name: str | None = None,
        covered_typenames: tuple[str, ...] = (),
    ) -> list[CollectedArtifact]:
        grouped = collect_conditional_fields(
            schema=self.schema,
            fragments=ctx.fragments,
            runtime_type=runtime_type,
            roots=roots,
        )
        # This model only ever validates a payload delivered under one of the
        # roots' conditions, so a key is required relative to those — a field
        # inside `address @include(if: $x) { city }` is required, not optional:
        # whenever the payload exists at all, so does the key.
        presence = [cond for _, cond in roots]
        if require_typename_for is not None:
            # This model is a variant of a discriminated union, and __typename
            # is its pydantic discriminator: a conditional one would render an
            # optional-Literal discriminator that pydantic rejects at import.
            typename_entries = grouped.get("__typename")
            if typename_entries is None:
                msg = (
                    f"Missing __typename in selection set for '{require_typename_for}'"
                )
                raise GraphQLGenerationError([msg])
            witness = uncovered_assignment(
                presence, [entry.cond for entry in typename_entries]
            )
            if witness is not None:
                msg = (
                    f"__typename in selection set for '{require_typename_for}' "
                    f"in '{ctx.query_name}' at {ctx.location} must be selected "
                    "unconditionally: it is the discriminator of a polymorphic "
                    "model"
                )
                raise GraphQLGenerationError([msg])
        child_models: list[CollectedArtifact] = []
        fields: list[CollectedField] = []
        for response_key, entries in grouped.items():
            _reject_conditionally_merged_slot(
                response_key=response_key,
                entries=entries,
                query_name=ctx.query_name,
                location=ctx.location,
            )
            field_child_models, collected_field = self._collect_field(
                model_name_base=model_name_base,
                runtime_type=runtime_type,
                response_key=response_key,
                entries=entries,
                ctx=ctx,
                typename_type=typename_type,
                is_conditional=uncovered_assignment(
                    presence, [entry.cond for entry in entries]
                )
                is not None,
            )
            child_models.extend(field_child_models)
            fields.append(collected_field)

        if not fields:
            # A selection set is syntactically non-empty, so nothing left here
            # means every field was dropped by @skip/@include conditions that
            # can never hold — literal arguments or a contradictory variable
            # pair. A fieldless class renders with an empty body that the
            # generated module cannot even import.
            msg = (
                f"Selection for '{model_name_base}' in '{ctx.query_name}' at "
                f"{ctx.location} is statically empty: every field is excluded "
                "by @skip/@include"
            )
            raise GraphQLGenerationError([msg])

        return [
            *child_models,
            CollectedModel(
                name=model_name_base,
                fields=fields,
                graphql_type_name=graphql_type_name,
                slot_name=slot_name,
                covered_typenames=covered_typenames,
            ),
        ]

    def _collect_field(
        self,
        *,
        model_name_base: str,
        runtime_type: graphql.GraphQLObjectType,
        response_key: str,
        entries: list[ConditionalNode],
        ctx: CollectionContext,
        typename_type: TypeRef | None,
        is_conditional: bool,
    ) -> tuple[list[CollectedArtifact], CollectedField]:
        name = python_field_name(response_key, self.to_snake_fn)
        if response_key == "__typename":
            type_info = typename_type or ScalarRef(
                expr=f'Literal["{runtime_type.name}"]'
            )
            return (
                [],
                CollectedField(
                    name=name,
                    response_key=response_key,
                    type_info=type_info,
                    is_conditional=is_conditional,
                ),
            )

        representative = entries[0].node
        field_def = cast(
            graphql.GraphQLField | None,
            get_field_def(self.schema, runtime_type, representative),
        )
        if field_def is None:
            msg = (
                f"Field '{representative.name.value}' not found in type "
                f"'{runtime_type.name}'"
            )
            raise ValueError(msg)
        warn_deprecated_field(ctx.query_name, runtime_type, representative, field_def)
        child_models, type_info = self._collect_typed_field(
            model_name_base=model_name_base,
            response_key=response_key,
            gql_type=field_type(field_def),
            entries=entries,
            ctx=ctx,
            # Any node of the response key, not just the representative: slot
            # collection and exec-source stripping both fire on any node with
            # the directive, and a response key merged from several nodes may
            # carry it on any of them.
            slot_name=response_key
            if any(has_slot_directive(entry.node) for entry in entries)
            else None,
        )
        return (
            child_models,
            CollectedField(
                name=name,
                response_key=response_key,
                type_info=type_info,
                is_conditional=is_conditional,
            ),
        )

    def _collect_typed_field(
        self,
        *,
        model_name_base: str,
        response_key: str,
        gql_type: graphql.GraphQLType,
        entries: list[ConditionalNode],
        ctx: CollectionContext,
        slot_name: str | None,
    ) -> tuple[list[CollectedArtifact], TypeRef]:
        # Each parent node contributes its subtree under its own condition:
        # a child selected through only one of the merged parents is exactly
        # as conditional as that parent.
        roots: SelectionRoots = tuple(
            (entry.node.selection_set, entry.cond)
            for entry in entries
            if entry.node.selection_set is not None
        )
        if not roots:
            return [], self.collect_type(gql_type)

        child_base = model_name_base + capitalize_first(response_key)
        if slot_name is not None:
            # Slot models are kept out of the rename pass, so this generated
            # name is the final one; the suffix marks the slot subtree's root.
            child_base += "Slot"
        return (
            self._collect_composite_model(
                base_name=child_base,
                named=graphql.get_named_type(gql_type),
                roots=roots,
                ctx=ctx,
                origin=f"field {response_key}",
                slot_name=slot_name,
            ),
            self.collect_type(gql_type, child_model_name=child_base),
        )

    def _collect_composite_model(
        self,
        *,
        base_name: str,
        # Optional because a type condition is looked up by name; an unresolved
        # one lands in the exhaustive branch below with the rest of the
        # non-composite types.
        named: graphql.GraphQLNamedType | None,
        roots: SelectionRoots,
        ctx: CollectionContext,
        origin: str,
        slot_name: str | None = None,
    ) -> list[CollectedArtifact]:
        # Shared by a composite field selection and by a fragment root: both
        # start from a named composite type plus selection roots, and both must
        # produce the same shape of models for it. The flat merge below feeds
        # the existence-based walks (explicit variants, base __typename), which
        # do not read conditions.
        merged_selections = graphql.SelectionSetNode(
            selections=[
                selection
                for selection_set, _ in roots
                for selection in selection_set.selections
            ]
        )
        match named:
            case graphql.GraphQLObjectType():
                return self._collect_object_model(
                    model_name_base=base_name,
                    runtime_type=named,
                    roots=roots,
                    ctx=ctx,
                    graphql_type_name=named.name,
                    slot_name=slot_name,
                    covered_typenames=(named.name,),
                )
            case graphql.GraphQLUnionType():
                possible = sorted(union_types(named), key=lambda typ: typ.name)
                return self._collect_polymorphic_models(
                    base_name=base_name,
                    possible_types=possible,
                    explicit_types=set(possible),
                    roots=roots,
                    ctx=ctx,
                    require_typename_for=named.name,
                    slot_name=slot_name,
                )
            case graphql.GraphQLInterfaceType():
                possible = sorted(
                    self.schema.get_possible_types(named),
                    key=lambda typ: typ.name,
                )
                if not possible:
                    msg = f"Interface '{named.name}' has no possible types"
                    raise ValueError(msg)
                explicit = resolve_explicit_types(
                    schema=self.schema,
                    selection_set=merged_selections,
                    fragments=ctx.fragments,
                    interface_type=named,
                    possible_types=possible,
                )
                if explicit and not interface_has_base_typename(
                    merged_selections, ctx.fragments, named.name
                ):
                    msg = (
                        f"Missing __typename in selection set for interface"
                        f" '{named.name}'"
                    )
                    raise GraphQLGenerationError([msg])
                return self._collect_polymorphic_models(
                    base_name=base_name,
                    possible_types=possible,
                    explicit_types=explicit,
                    roots=roots,
                    ctx=ctx,
                    require_typename_for=named.name,
                    slot_name=slot_name,
                )
            case _:
                msg = f"Unknown type {named} for {origin}"
                raise ValueError(msg)

    def _collect_polymorphic_models(
        self,
        *,
        base_name: str,
        possible_types: list[graphql.GraphQLObjectType],
        explicit_types: set[graphql.GraphQLObjectType],
        roots: SelectionRoots,
        ctx: CollectionContext,
        require_typename_for: str,
        slot_name: str | None,
    ) -> list[CollectedArtifact]:
        if not explicit_types:
            if slot_name is None:
                typename_type: TypeRef = ScalarRef(expr="str", name_hint="Str")
            else:
                # A slot node's typename is closed to the schema snapshot: an
                # implementation the snapshot does not know must fail loudly —
                # the union slot already does via its discriminated Literal,
                # and an open `str` here would let the same drift pass through
                # and silently drop data the server sent. This is also what
                # makes a covered_typenames__ miss on the slot root a plain
                # mismatch: drift fails here first (pinned by
                # test_schema_drift_typename_fails_loudly_on_interface_slot).
                names = ", ".join(repr(typ.name) for typ in possible_types)
                typename_type = ScalarRef(expr=f"Literal[{names}]")
            return self._collect_object_model(
                model_name_base=base_name,
                runtime_type=possible_types[0],
                roots=roots,
                ctx=ctx,
                typename_type=typename_type,
                graphql_type_name=require_typename_for,
                slot_name=slot_name,
                covered_typenames=tuple(typ.name for typ in possible_types),
            )

        # possible_types is already sorted by name; preserve that order for
        # explicit variants and for the fallback group instead of rebuilding it
        # through set→sorted/min round-trips.
        child_models: list[CollectedArtifact] = []
        union_types: list[str] = []
        fallback_objects: list[graphql.GraphQLObjectType] = []
        for object_type in possible_types:
            if object_type not in explicit_types:
                fallback_objects.append(object_type)
                continue
            model_name = base_name + object_type.name
            child_models.extend(
                self._collect_object_model(
                    model_name_base=model_name,
                    runtime_type=object_type,
                    roots=roots,
                    ctx=ctx,
                    require_typename_for=require_typename_for,
                    graphql_type_name=object_type.name,
                    slot_name=slot_name,
                    covered_typenames=(object_type.name,),
                )
            )
            union_types.append(model_name)

        if fallback_objects:
            fallback_name = base_name + require_typename_for
            fallback_typename = ScalarRef(
                expr=f"Literal[{', '.join(repr(obj.name) for obj in fallback_objects)}]"
            )
            child_models.extend(
                self._collect_object_model(
                    model_name_base=fallback_name,
                    runtime_type=fallback_objects[0],
                    roots=roots,
                    ctx=ctx,
                    typename_type=fallback_typename,
                    require_typename_for=require_typename_for,
                    graphql_type_name=require_typename_for,
                    slot_name=slot_name,
                    covered_typenames=tuple(obj.name for obj in fallback_objects),
                )
            )
            union_types.append(fallback_name)

        return [
            *child_models,
            CollectedUnionAlias(
                name=base_name,
                variants=tuple(union_types),
                discriminator="typename__",
            ),
        ]


class _NamedStatement(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def stmt(self) -> Statement: ...


def _dedup_statements[T: _NamedStatement](
    items: list[T], kind: str
) -> tuple[list[T], dict[str, list[str]], dict[str, list[str]]]:
    # Shared by operations and fragment statements: identical (dedented) text
    # under one name is a harmless copy, differing text is ambiguous. Every
    # distinct literal spelling is kept per name — the dispatch dict is keyed
    # by the exact literal, so each spelling needs its own entry.
    all_locations: dict[str, list[str]] = defaultdict(list)
    spellings: dict[str, dict[str, None]] = defaultdict(dict)
    first_occurrence: dict[str, T] = {}
    for item in items:
        all_locations[item.name].append(item.stmt.location)
        spellings[item.name][item.stmt.raw_text] = None
        if item.name not in first_occurrence:
            first_occurrence[item.name] = item
        elif first_occurrence[item.name].stmt.hash_str != item.stmt.hash_str:
            msg = (
                f"Cannot compile different GraphQL {kind} with same name "
                f"{item.name} at {', '.join(all_locations[item.name])}"
            )
            raise GraphQLGenerationError([msg])
    unique = sorted(
        first_occurrence.values(),
        key=lambda item: (item.stmt.file, item.stmt.lineno),
    )
    return (
        unique,
        dict(all_locations),
        {name: list(texts) for name, texts in spellings.items()},
    )


def collect_package_ir(
    *,
    schema: graphql.GraphQLSchema,
    queries: list[Query],
    fragment_statements: list[FragmentStatement],
    scalars: dict[str, ImportRef],
    to_snake_fn: StrTransform,
) -> CollectedPackageIR:
    queries, all_locations, query_spellings = _dedup_statements(queries, "queries")
    # Every type a slot field resolves to gets a compatibility base; the
    # statements arriving here are already exactly the ones some slot can
    # accept — `ParseResult.reachable_statements`, the single place that
    # predicate lives.
    slot_types = tuple(
        sorted({slot.type_name for query in queries for slot in query.slots})
    )
    fragment_statements, _, fragment_spellings = _dedup_statements(
        fragment_statements, "fragments"
    )

    collector = PackageCollector(
        schema=schema,
        scalars=scalars,
        to_snake_fn=to_snake_fn,
    )
    result_artifacts: list[CollectedArtifact] = []
    for query in queries:
        result_artifacts.extend(collector.collect_operation_models(query))
    for statement in fragment_statements:
        result_artifacts.extend(collector.collect_fragment_models(statement))
    input_artifacts = collect_input_artifacts(
        collect_input_type_closure(queries),
        to_snake_fn=collector.to_snake_fn,
        collect_type=collector.collect_type,
    )
    operations = _collect_operations(collector, queries, all_locations, query_spellings)
    fragments = _collect_fragments(
        collector, fragment_statements, slot_types, fragment_spellings
    )

    # Validated before any name-keyed structure is derived: the subtree walk
    # below and the rename pass both resolve NamedRefs through these names.
    name_errors = validate_collected_names(
        result_artifacts=result_artifacts,
        input_artifacts=input_artifacts,
        fragments=fragments,
        enums=[collector.enums[name] for name in sorted(collector.enums)],
    )
    if name_errors:
        raise GraphQLGenerationError(name_errors)

    open_model_names, fragments = _resolve_subtrees(result_artifacts, fragments)
    return CollectedPackageIR(
        result_artifacts=result_artifacts,
        input_artifacts=input_artifacts,
        operations=operations,
        fragments=fragments,
        slot_types=slot_types,
        enums=[collector.enums[name] for name in sorted(collector.enums)],
        open_model_names=open_model_names,
    )


def _covered_typenames(
    root: str, artifacts: dict[str, CollectedArtifact]
) -> frozenset[str]:
    # The runtime typenames a fragment's selection covers. A uniform model
    # covers several typenames with one selection; an alias contributes each
    # of its variants' own.
    match artifacts[root]:
        case CollectedModel() as model:
            if not model.covered_typenames:
                # Internal invariant: every model this walk reaches covers at
                # least one runtime typename — an empty cover would turn every
                # read of the handle into a silent None.
                msg = f"fragment model {root!r} covers no runtime typenames"
                raise AssertionError(msg)
            return frozenset(model.covered_typenames)
        case CollectedUnionAlias() as alias:
            covered: frozenset[str] = frozenset()
            for variant in alias.variants:
                covered |= _covered_typenames(variant, artifacts)
            return covered


def _resolve_subtrees(
    result_artifacts: list[CollectedArtifact],
    fragments: list[CollectedFragment],
) -> tuple[frozenset[str], list[CollectedFragment]]:
    # A slot payload carries every passed fragment's fields next to the static
    # selection, and every fragment validates that same payload — so each
    # model inside a slot or fragment subtree tolerates the keys other readers
    # asked for (the open base) while its typed fields keep isolation intact.
    # Each handle also snapshots the typenames its root covers.
    artifacts_by_name = {artifact.name: artifact for artifact in result_artifacts}
    open_names: set[str] = set()
    for model, _ in slot_roots(result_artifacts):
        open_names.add(model.name)
        open_names |= reachable_model_names(model.name, artifacts_by_name)
    for fragment in fragments:
        open_names.add(fragment.model_name)
        open_names |= reachable_model_names(fragment.model_name, artifacts_by_name)
    fragments = [
        replace(
            fragment,
            covered_typenames=_covered_typenames(
                fragment.model_name, artifacts_by_name
            ),
        )
        for fragment in fragments
    ]
    return frozenset(open_names), fragments


def compatible_base_names(
    schema: graphql.GraphQLSchema, fragment_type: str, slot_types: tuple[str, ...]
) -> tuple[str, ...]:
    # One base per slot type the fragment can spread into, per the canonical
    # rule in `spreads_into`.
    return tuple(
        fragment_base_name(slot_type)
        for slot_type in slot_types
        if spreads_into(schema, fragment_type, slot_type)
    )


def _collect_fragments(
    collector: PackageCollector,
    statements: list[FragmentStatement],
    slot_types: tuple[str, ...],
    spellings: dict[str, list[str]],
) -> list[CollectedFragment]:
    return [
        CollectedFragment(
            stmt_texts=tuple(spellings[statement.name]),
            location=statement.stmt.location,
            class_name=capitalize_first(statement.name),
            singleton_name=collector.to_snake_fn(statement.name).upper(),
            fragment_name=statement.name,
            model_name=fragment_model_name(statement.name),
            definition_text=statement.definition_text,
            # A handle compatible with no slot in the package derives the
            # runtime class directly, and is then rejected at every slot kwarg.
            base_names=compatible_base_names(
                collector.schema, statement.type_condition, slot_types
            )
            or ("slots.GQLFragment",),
        )
        for statement in statements
    ]


def _collect_operations(
    collector: PackageCollector,
    queries: list[Query],
    all_locations: dict[str, list[str]],
    spellings: dict[str, list[str]],
) -> list[CollectedOperation]:
    operations: list[CollectedOperation] = []
    for query in queries:
        variables = tuple(
            CollectedOperationVar(
                gql_name=variable.name,
                python_name=collector.to_snake_fn(variable.name),
                type_info=collector.collect_type(variable.gql_type),
                default_expr=(
                    repr(variable.default_value)
                    if variable.default_value != graphql.Undefined
                    else None
                ),
            )
            for variable in query.variables
        )
        slots = tuple(
            CollectedSlot(
                name=slot.name,
                python_name=collector.to_snake_fn(slot.name),
                base_name=fragment_base_name(slot.type_name),
            )
            for slot in query.slots
        )
        class_name = capitalize_first(query.name)
        operations.append(
            CollectedOperation(
                stmt_texts=tuple(spellings[query.name]),
                class_name=class_name,
                result_type=f"{class_name}Result",
                exec_head=query.exec_head,
                exec_splices=query.exec_splices,
                variables=variables,
                slots=slots,
                is_subscription=(
                    query.operation_def.operation == graphql.OperationType.SUBSCRIPTION
                ),
                locations=tuple(all_locations[query.name]),
            )
        )
    return operations
