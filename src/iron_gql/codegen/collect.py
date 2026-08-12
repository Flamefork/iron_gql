import dataclasses
from collections import defaultdict
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Protocol
from typing import cast
from warnings import warn

import graphql
from graphql.execution.execute import get_field_def

from iron_gql.codegen.accessors import field_type
from iron_gql.codegen.accessors import union_types
from iron_gql.codegen.accessors import wrapping_of_type
from iron_gql.codegen.bindings import ExpandedBinding
from iron_gql.codegen.bindings import OmittableSynthesizedVar
from iron_gql.codegen.bindings import ReadableFragment
from iron_gql.codegen.bindings import RequiredSynthesizedVar
from iron_gql.codegen.bindings import SlotTarget
from iron_gql.codegen.bindings import expand_binding
from iron_gql.codegen.bindings import fragment_closure
from iron_gql.codegen.bindings import fragment_own_vars
from iron_gql.codegen.bindings import unknown_slot_error
from iron_gql.codegen.collect_inputs import collect_input_artifacts
from iron_gql.codegen.collect_inputs import collect_input_type_closure
from iron_gql.codegen.combinations import Combination
from iron_gql.codegen.combinations import compatible_fragment_names
from iron_gql.codegen.combinations import enumerate_combinations
from iron_gql.codegen.discovery import BindDecl
from iron_gql.codegen.discovery import BindKeywordCheck
from iron_gql.codegen.discovery import Statement
from iron_gql.codegen.ir import BUILTIN_SCALARS
from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedBinding
from iron_gql.codegen.ir import CollectedBindingSlot
from iron_gql.codegen.ir import CollectedEnum
from iron_gql.codegen.ir import CollectedFactoryFragment
from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedFragment
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedOmittableFragmentArg
from iron_gql.codegen.ir import CollectedOnTypeBase
from iron_gql.codegen.ir import CollectedOperation
from iron_gql.codegen.ir import CollectedOperationVar
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import CollectedPlainFragment
from iron_gql.codegen.ir import CollectedReadableFragment
from iron_gql.codegen.ir import CollectedRequiredFragmentArg
from iron_gql.codegen.ir import CollectedTemplate
from iron_gql.codegen.ir import CollectedTemplateSlot
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ImportRef
from iron_gql.codegen.ir import ListRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import StrTransform
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.ir import applied_fragment_class_name
from iron_gql.codegen.ir import make_optional
from iron_gql.codegen.ir import on_type_base_name
from iron_gql.codegen.ir import result_model_name
from iron_gql.codegen.ir import slot_param_name
from iron_gql.codegen.ir import slot_roots
from iron_gql.codegen.names import validate_collected_names
from iron_gql.codegen.parser import FragmentStatement
from iron_gql.codegen.parser import GQLVar
from iron_gql.codegen.parser import Operation
from iron_gql.codegen.parser import Query
from iron_gql.codegen.parser import Template
from iron_gql.codegen.parser import parse_var
from iron_gql.codegen.selection import ALWAYS
from iron_gql.codegen.selection import ConditionalNode
from iron_gql.codegen.selection import SelectionRoots
from iron_gql.codegen.selection import collect_conditional_fields
from iron_gql.codegen.selection import interface_has_base_typename
from iron_gql.codegen.selection import resolve_explicit_types
from iron_gql.codegen.selection import uncovered_assignment
from iron_gql.codegen.slots import QuerySlot
from iron_gql.codegen.slots import has_slot_directive
from iron_gql.codegen.slots import reachable_model_names
from iron_gql.codegen.util import capitalize_first
from iron_gql.codegen.util import reachable
from iron_gql.codegen.warnings import GraphQLDeprecationWarning
from iron_gql.codegen.warnings import UnknownGQLTypeWarning
from iron_gql.codegen.warnings import warn_deprecated_field
from iron_gql.slots import combination_key


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
    # The invariant: the slot's fragment spreads are inserted into the
    # expanded operation exactly where a @slot node selects the key, so no
    # variable assignment may keep the key in the response while excluding
    # every @slot node — the key would arrive without the fragments' fields.
    # The slot field itself cannot carry @skip/@include, so its condition
    # here is purely inherited from conditional parents the node is nested
    # under; the check runs over the exact condition of every node merged
    # into the key.
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
                # Internal invariant: a GraphQL type is either named or one of
                # the two wrappers above. The case stays because graphql-core
                # spells `GraphQLType` as a base class rather than a union, so
                # no type checker can see the three as exhausting it.
                msg = f"Unsupported GraphQL type: {gql_type}"
                raise AssertionError(msg)
        if nullable:
            return make_optional(typ)
        return typ

    def field_type_ref(
        self,
        gql_type: graphql.GraphQLNamedType,
    ) -> TypeRef:
        match gql_type:
            case graphql.GraphQLScalarType(name=name):
                # The name hint travels with every scalar, mapped or builtin:
                # it is what `naming._model_type_name_tokens` distinguishes two
                # shapes of one GraphQL type by, and a scalar that contributed
                # no token made `User{id, name}` and `User{id, name | None}`
                # collide under one detailed name.
                if name in self.scalars:
                    return ScalarRef(
                        expr=self.scalars[name].dotted_path, name_hint=name
                    )
                if name in BUILTIN_SCALARS:
                    return ScalarRef(expr=BUILTIN_SCALARS[name], name_hint=name)
                warn(
                    f"Unknown scalar type: {name}, mapped to 'object'",
                    category=UnknownGQLTypeWarning,
                    stacklevel=1,
                )
                return ScalarRef(expr="object", name_hint="Object")
            case graphql.GraphQLInputObjectType(name=name):
                return NamedRef(name=name)
            case graphql.GraphQLEnumType():
                self.collect_enum(gql_type)
                return NamedRef(name=gql_type.name)
            case _:
                # Internal invariant: what is left of the named types are the
                # composite ones, and a composite never arrives here. An output
                # position reaches this only through `_collect_typed_field`'s
                # no-selection-set branch, which validation (`ScalarLeafs`)
                # leaves to the leaf types alone; an input position is a scalar,
                # an enum or an input object by the schema's own rules.
                msg = f"Unsupported named GraphQL type: {gql_type}"
                raise AssertionError(msg)

    def collect_enum(
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
            model_name_base=result_model_name(query.class_name),
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
            # Internal invariant: `collect_conditional_fields` only groups a
            # node whose enclosing type condition matches `runtime_type`, and a
            # validated document (`FieldsOnCorrectType`) selects on that type
            # nothing the type does not have.
            msg = (
                f"Field '{representative.name.value}' not found in type "
                f"'{runtime_type.name}'"
            )
            raise AssertionError(msg)
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
        # Optional only because `schema.get_type` is: both call sites hand over
        # a type a validated document has already pinned to a composite one --
        # a fragment's type condition and a field's named type under a
        # selection set -- so None reaches the invariant branch below and
        # nothing else.
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
                    # A diagnosis, not an invariant: an interface no type
                    # implements is a legal schema, and selecting a field of
                    # that type is a legal document -- graphql-core validates
                    # both. There is simply no object type whose payload a
                    # model could describe, and no `__typename` the server
                    # could ever answer with.
                    msg = (
                        f"Interface '{named.name}' selected by {origin} in "
                        f"'{ctx.query_name}' at {ctx.location} has no "
                        "implementing type in the schema; no payload can ever "
                        "arrive for it -- drop the selection, or implement the "
                        "interface"
                    )
                    raise GraphQLGenerationError([msg])
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
                # Internal invariant: a fragment's type condition is checked by
                # `FragmentsOnCompositeTypes` and a field carrying a selection
                # set by `ScalarLeafs`, both before anything is collected, so
                # the name resolved and it resolved to one of the three
                # composite kinds above.
                msg = f"Unsupported type {named} for {origin}"
                raise AssertionError(msg)

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
                )
            )
            union_types.append(fallback_name)

        return [
            *child_models,
            CollectedUnionAlias(
                name=base_name,
                variants=tuple(NamedRef(name=name) for name in union_types),
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


@dataclass(kw_only=True, frozen=True)
class _PreparedPackage:
    # Deduplicated and partitioned once, up front: everything downstream in
    # `collect_package_ir` reads from here instead of re-deriving it.
    operation_queries: list[Operation]
    template_queries: list[Template]
    fragment_statements: list[FragmentStatement]
    query_locations: dict[str, list[str]]
    fragment_locations: dict[str, list[str]]
    query_spellings: dict[str, list[str]]
    fragment_spellings: dict[str, list[str]]
    # Keyed by the pre-dedup Statement identity: a bind's `template` /
    # `slot_args` are resolved against discovery's own raw scan, and dedup
    # may collapse several equal-text Statements onto one canonical
    # Template/FragmentStatement — so the lookup has to use the original list.
    # Templates only: `parser.validate_bind_templates` has already rejected
    # every bind whose base is anything else.
    template_by_stmt: dict[Statement, Template]
    fragment_by_stmt: dict[Statement, FragmentStatement]


def _prepare_package(
    operations: list[Operation],
    templates: list[Template],
    discovered_fragments: list[FragmentStatement],
) -> _PreparedPackage:
    # Built off the discovered lists, before dedup collapses equal-text
    # statements: a bind names the raw `Statement` discovery saw, so only the
    # pre-dedup identity resolves it. Named apart from the deduplicated lists
    # below rather than shadowed by them, so neither can be read as the other.
    template_by_stmt = {query.stmt: query for query in templates}
    fragment_by_stmt = {statement.stmt: statement for statement in discovered_fragments}

    # Deduplicated over both kinds at once: an operation and a template
    # sharing a name is the same clash as two operations sharing one, and the
    # generated module has one namespace for both.
    queries, query_locations, query_spellings = _dedup_statements(
        [*operations, *templates], "queries"
    )
    canonical_fragments, fragment_locations, fragment_spellings = _dedup_statements(
        discovered_fragments, "fragments"
    )
    return _PreparedPackage(
        operation_queries=[q for q in queries if isinstance(q, Operation)],
        template_queries=[q for q in queries if isinstance(q, Template)],
        fragment_statements=canonical_fragments,
        query_locations=query_locations,
        fragment_locations=fragment_locations,
        query_spellings=query_spellings,
        fragment_spellings=fragment_spellings,
        template_by_stmt=template_by_stmt,
        fragment_by_stmt=fragment_by_stmt,
    )


def _collect_result_artifacts(
    collector: PackageCollector,
    operation_queries: list[Operation],
    template_queries: list[Template],
    fragment_statements: list[FragmentStatement],
) -> tuple[list[CollectedArtifact], dict[str, list[CollectedArtifact]]]:
    # Each template's own artifacts are kept aside, keyed by its operation
    # name: which of them belong to which slot is written on the artifacts
    # themselves (`CollectedModel.slot_name`), but *which template* collected
    # them is only knowable here, where one walk's output is still one list.
    result_artifacts: list[CollectedArtifact] = []
    template_artifacts: dict[str, list[CollectedArtifact]] = {}
    for query in operation_queries:
        result_artifacts.extend(collector.collect_operation_models(query))
    for query in template_queries:
        artifacts = collector.collect_operation_models(query)
        result_artifacts.extend(artifacts)
        template_artifacts[query.name] = artifacts
    for statement in fragment_statements:
        result_artifacts.extend(collector.collect_fragment_models(statement))
    return result_artifacts, template_artifacts


@dataclass(kw_only=True, frozen=True)
class _ExpandedCombination:
    combination: Combination
    template: CollectedTemplate
    expanded: ExpandedBinding
    # The synthesized fragment variables, parsed against the schema: their
    # types feed the input-artifact closure below (`CollectedFragment.
    # arg_vars`, built per fragment rather than per combination, is what
    # `with_args`'s own keywords come from).
    arg_gql_vars: list[GQLVar]
    # Места, редактирование которых создаёт или удаляет combination. Для явно
    # записанного combination это call sites `.bind(...)`; для полученного из
    # schema enumeration — statement template и statements всех spread
    # fragments. Диагностика должна указывать на фактический источник, иначе
    # разработчика отправит к template, который он не менял.
    locations: tuple[str, ...]


def _literal_combinations(
    *,
    binds: list[BindDecl],
    template_by_stmt: dict[Statement, Template],
    fragment_by_stmt: dict[Statement, FragmentStatement],
    template_by_name: dict[str, CollectedTemplate],
) -> dict[Combination, tuple[str, ...]]:
    # Каждый call site `.bind(...)` как combination вместе с записавшими его
    # locations. Только multi-fragment tuple добавляет то, чего не создаёт
    # enumeration. Но каждый bind сохраняется и для `expand_binding`, где
    # диагностируется keyword без соответствующего slot, а явно записанный
    # combination хранит locations для точной диагностики.
    #
    # Keyed by the combination, so two call sites spelling one combination
    # (`bind(a=x)` and `bind(a=x, b=[])`) meet on one entry carrying both
    # lines. `binds` already arrives in `(file, lineno)` order --
    # `discover_package` sorts it there, at the one place binds enter the
    # pipeline -- so both the keys and each entry's locations inherit one
    # deterministic order without re-sorting.
    literal: dict[Combination, tuple[str, ...]] = {}
    for bind in binds:
        template = template_by_name[template_by_stmt[bind.template].name]
        written = {
            key: tuple(sorted(fragment_by_stmt[stmt].name for stmt in stmts))
            for key, stmts in bind.slot_args
        }
        # Every slot of the template first, in its order, so a bind spelling a
        # combination the enumeration also produces is equal to it and merges
        # away; then whatever keywords are left, which name no slot at all --
        # `expand_binding` is what says so.
        on_slots = [
            (slot.python_name, written.pop(slot.python_name, ()))
            for slot in template.slots
        ]
        combination = Combination(
            template_name=template.name,
            slots=(*on_slots, *sorted(written.items())),
        )
        literal[combination] = (*literal.get(combination, ()), *bind.locations)
    return literal


def _validate_bind_keyword_checks(
    *,
    checks: Sequence[BindKeywordCheck],
    prepared: _PreparedPackage,
    templates: list[CollectedTemplate],
) -> None:
    templates_by_name = {template.name: template for template in templates}
    errors: list[str] = []
    for check in checks:
        parsed = prepared.template_by_stmt[check.template]
        slot_names = {slot.python_name for slot in templates_by_name[parsed.name].slots}
        errors.extend(
            unknown_slot_error(
                key=keyword,
                slot_names=slot_names,
                location=check.location,
            )
            for keyword in check.keywords
            if keyword not in slot_names
        )
    if errors:
        raise GraphQLGenerationError(errors)


def _enumerate_and_expand(
    *,
    schema: graphql.GraphQLSchema,
    prepared: _PreparedPackage,
    collected_templates: list[CollectedTemplate],
    fragment_defs: dict[str, graphql.FragmentDefinitionNode],
    binds: list[BindDecl],
) -> list[_ExpandedCombination]:
    # Определяет существующие combinations и document каждого из них. Это один
    # шаг: enumeration решает, что разворачивать, а expansion запускает
    # validations отдельно для каждой пары, а не для каждого call site.
    #
    # Everything here keys templates by their GraphQL operation name, which
    # `_dedup_statements` has already made injective -- unlike `class_name`,
    # which two operation names differing only in the first letter's case
    # collapse onto, silently answering one template's combination with the
    # other template's slots.
    by_operation_name = {template.name: template for template in collected_templates}
    query_by_operation_name = {query.name: query for query in prepared.template_queries}
    literal = _literal_combinations(
        binds=binds,
        template_by_stmt=prepared.template_by_stmt,
        fragment_by_stmt=prepared.fragment_by_stmt,
        template_by_name=by_operation_name,
    )
    return _expand_combinations(
        schema=schema,
        combinations=enumerate_combinations(
            schema=schema,
            templates=collected_templates,
            fragments=fragment_defs,
            literal_binds=list(literal),
        ),
        template_queries=query_by_operation_name,
        template_by_name=by_operation_name,
        fragment_statements=prepared.fragment_statements,
        fragment_locations=prepared.fragment_locations,
        literal_locations=literal,
    )


def _expand_combinations(
    *,
    schema: graphql.GraphQLSchema,
    combinations: list[Combination],
    template_queries: dict[str, Template],
    template_by_name: dict[str, CollectedTemplate],
    fragment_statements: list[FragmentStatement],
    fragment_locations: dict[str, list[str]],
    literal_locations: dict[Combination, tuple[str, ...]],
) -> list[_ExpandedCombination]:
    # Every fragment a combination's closure can reach is already in
    # `fragment_statements` -- `parser.bindable_statements` is exactly the set
    # the enumeration draws from -- so this is a complete namespace for
    # `expand_binding`'s own closure resolution.
    all_fragment_defs = {
        statement.name: statement.definition for statement in fragment_statements
    }
    expanded_combinations: list[_ExpandedCombination] = []
    # Accumulated across every combination, like parser.py's validate_bind_*
    # family: a user whose package produces several broken pairs sees every
    # diagnosis in one regeneration instead of fixing them one at a time.
    errors: list[str] = []
    for combination in combinations:
        query = template_queries[combination.template_name]
        template = template_by_name[combination.template_name]
        spreads = {
            key: tuple(all_fragment_defs[name] for name in names)
            for key, names in combination.slots
        }
        # Keyed by the keyword a caller writes -- the slot's `python_name`,
        # which is what `bind(...)` renders its parameters as -- so the check
        # against the template's slots and the diagnosis when it fails both
        # speak the spelling the source uses. `expand_binding` translates to
        # the response key on its way into the document.
        slots = {
            slot.python_name: SlotTarget(
                type_name=slot.type_name, response_key=slot.name
            )
            for slot in template.slots
        }
        locations = _combination_locations(
            query,
            combination,
            fragment_locations=fragment_locations,
            literal_locations=literal_locations,
        )
        try:
            expanded = expand_binding(
                schema=schema,
                template_doc=query.doc,
                template_operation=query.operation_def,
                template_name=query.name,
                slots=slots,
                spreads=spreads,
                all_fragments=all_fragment_defs,
                location=", ".join(locations),
            )
        except GraphQLGenerationError as exc:
            errors.extend(exc.errors)
            continue
        expanded_combinations.append(
            _ExpandedCombination(
                combination=combination,
                template=template,
                expanded=expanded,
                arg_gql_vars=[
                    parse_var(var.node, schema=schema, context=", ".join(locations))
                    for var in expanded.fragment_vars
                ],
                locations=locations,
            )
        )
    if errors:
        raise GraphQLGenerationError(errors)
    return expanded_combinations


def _combination_locations(
    query: Template,
    combination: Combination,
    *,
    fragment_locations: dict[str, list[str]],
    literal_locations: dict[Combination, tuple[str, ...]],
) -> tuple[str, ...]:
    # A combination somebody wrote answers with the lines they wrote: that is
    # the only edit that removes it, and a diagnosis naming the template
    # instead sends them to a file they never touched. Every other combination
    # exists because the template's statement and its fragments' statements
    # exist, so those are what it names -- the template first, its fragments in
    # slot order. A fragment discovered under several spellings contributes
    # every one of its locations, the same contract `CollectedFragment.
    # locations` carries.
    written = literal_locations.get(combination)
    if written is not None:
        return written
    return (
        query.stmt.location,
        *(
            location
            # `dict.fromkeys` rather than a set: one fragment may fill two
            # slots of one combination, and the order has to stay the slots'.
            for name in dict.fromkeys(
                name for _key, names in combination.slots for name in names
            )
            for location in fragment_locations[name]
        ),
    )


def _collect_input_artifacts_with_binds(
    collector: PackageCollector,
    queries: Sequence[Query],
    expanded_combinations: list[_ExpandedCombination],
    *,
    fragment_gql_vars: list[GQLVar],
) -> list[CollectedArtifact]:
    # A binding's synthesized fragment variables can introduce an input type
    # no query declares on its own -- and so can a factory fragment's own
    # variables, independent of whether any combination ever reaches it: a
    # factory compatible with no slot in the package still renders a
    # `with_args`, and an input-object-typed parameter there needs its class
    # defined somewhere. `fragment_gql_vars` is every factory's own variables
    # (`collect._collect_fragments`), which is always a superset of what any
    # one combination's `arg_gql_vars` contributes -- both are kept, rather
    # than dropping the combination-level walk now that it is redundant,
    # since consolidating the two is a bigger change than this gap calls for.
    extra_var_types = [
        gql_var.gql_type
        for expanded in expanded_combinations
        for gql_var in expanded.arg_gql_vars
    ] + [gql_var.gql_type for gql_var in fragment_gql_vars]
    artifacts = collect_input_artifacts(
        collect_input_type_closure(queries, extra_types=extra_var_types),
        to_snake_fn=collector.to_snake_fn,
        collect_type=collector.collect_type,
    )
    # The closure above answers with input *objects*, so an enum a fragment
    # variable names on its own -- reached through no input object and through
    # no query variable -- would never be collected here. It is collected once
    # more when the bindings themselves are built, but that happens after the
    # package's enum list is taken, and the module then names a type it never
    # declares.
    #
    # Only the enums, not the whole type: `collect_type` also diagnoses -- an
    # unconfigured custom scalar warns from it -- and `_collect_bindings` walks
    # these very types again, so anything else asked for here is asked twice
    # and the developer reads the same warning twice.
    for gql_type in extra_var_types:
        named = graphql.get_named_type(gql_type)
        if isinstance(named, graphql.GraphQLEnumType):
            collector.collect_enum(named)
    return artifacts


def collect_package_ir(
    *,
    schema: graphql.GraphQLSchema,
    operations: list[Operation],
    templates: list[Template],
    fragment_statements: list[FragmentStatement],
    binds: list[BindDecl],
    bind_keyword_checks: Sequence[BindKeywordCheck],
    discovered_texts: tuple[str, ...],
    scalars: dict[str, ImportRef],
    to_snake_fn: StrTransform,
) -> CollectedPackageIR:
    prepared = _prepare_package(operations, templates, fragment_statements)
    collector = PackageCollector(
        schema=schema, scalars=scalars, to_snake_fn=to_snake_fn
    )
    # `fragment_closure` внутри `_collect_fragments` использует полный набор
    # slot types ещё до построения template IR.
    slot_type_names = frozenset(
        slot.type_name for query in prepared.template_queries for slot in query.slots
    )

    result_artifacts, template_artifacts = _collect_result_artifacts(
        collector,
        prepared.operation_queries,
        prepared.template_queries,
        prepared.fragment_statements,
    )
    # Named IR first: expanding a bind resolves its keywords against the
    # templates' slot `python_name`s, so the templates -- and the check that
    # those names are unambiguous -- have to exist before any bind is read.
    collected_operations = _collect_operations(
        collector,
        prepared.operation_queries,
        prepared.query_locations,
        prepared.query_spellings,
    )
    fragments, fragment_gql_vars = _collect_fragments(
        collector,
        prepared.fragment_statements,
        prepared.fragment_locations,
        prepared.fragment_spellings,
        schema=schema,
        slot_type_names=slot_type_names,
    )
    # The package's fragment namespace, which is both what a slot's
    # compatibility is measured against and what the enumeration draws its
    # combinations from.
    fragment_defs = {
        statement.name: statement.definition
        for statement in prepared.fragment_statements
    }
    collected_templates = _collect_templates(
        collector,
        prepared.template_queries,
        prepared.query_locations,
        prepared.query_spellings,
        template_artifacts,
        fragment_defs=fragment_defs,
    )
    _validate_bind_keyword_checks(
        checks=bind_keyword_checks,
        prepared=prepared,
        templates=collected_templates,
    )
    expanded_combinations = _enumerate_and_expand(
        schema=schema,
        prepared=prepared,
        collected_templates=collected_templates,
        fragment_defs=fragment_defs,
        binds=binds,
    )
    input_artifacts = _collect_input_artifacts_with_binds(
        collector,
        [*prepared.operation_queries, *prepared.template_queries],
        expanded_combinations,
        fragment_gql_vars=fragment_gql_vars,
    )
    # Validated before any name-keyed structure is derived: the binding
    # lookups after this, the subtree walk, and the rename pass all resolve
    # names through these.
    enums = [collector.enums[name] for name in sorted(collector.enums)]
    name_errors = validate_collected_names(
        result_artifacts=result_artifacts,
        input_artifacts=input_artifacts,
        fragments=fragments,
        enums=enums,
    )
    if name_errors:
        raise GraphQLGenerationError(name_errors)

    bindings = _collect_bindings(
        expanded_combinations=expanded_combinations,
        collected_fragment_by_name={f.fragment_name: f for f in fragments},
    )

    return CollectedPackageIR(
        result_artifacts=result_artifacts,
        input_artifacts=input_artifacts,
        operations=collected_operations,
        fragments=fragments,
        on_type_bases=_collect_on_type_bases(
            fragment_type_names=(
                statement.type_condition for statement in prepared.fragment_statements
            ),
        ),
        templates=collected_templates,
        bindings=bindings,
        enums=enums,
        open_model_names=_open_model_names(result_artifacts, fragments),
        discovered_texts=discovered_texts,
    )


def parametrize_slot_paths(ir: CollectedPackageIR) -> CollectedPackageIR:
    # Один generic по slots набор result models на template вместо копии на
    # каждый binding. Readable fragments передаются как type argument result,
    # поэтому generic-код сохраняет форму result и может читать slot через
    # type-erased fragment reader.
    #
    # Parametrised are exactly the artifacts a slot node is reachable from, the
    # node itself included: what sits *below* a node is the slot's static
    # selection, which no binding varies. A template nothing binds is
    # parametrised all the same -- its models simply have no binding naming
    # them, and a bare `{Op}Result` reads as `Never` in every slot, which is
    # what "nothing is readable there" means.
    #
    # Runs after `apply_rename` (the names must be final) and after
    # `slots.validate_no_nested_slots` (which walks a template's own result
    # subtree, and nesting is a fact about that subtree).
    dependents: dict[str, list[str]] = {}
    for artifact in ir.result_artifacts:
        for dep in artifact.dependencies:
            dependents.setdefault(dep, []).append(artifact.name)
    params: dict[str, list[str]] = {}
    for template in ir.templates:
        # Template slot order is parameter order, so a binding can fill the
        # arguments straight from its own slots (which stand in that same
        # order) without either side sorting.
        for slot in template.slots:
            param = slot_param_name(slot.python_name)
            # Unioned with the roots because `reachable` counts only what an
            # edge leads to, and a node model is its own root here. Slot paths
            # are per-template by construction -- their shapes reference the
            # template's own slot model names, which no rename moves.
            seeds = list(slot.node_types)
            path = set(seeds) | reachable(seeds, lambda name: dependents.get(name, ()))
            for name in path:
                on_artifact = params.setdefault(name, [])
                # One slot name may be selected under two parents, and both
                # positions carry the same phantom: the second visit adds
                # nothing.
                if param not in on_artifact:
                    on_artifact.append(param)
    return dataclasses.replace(
        ir,
        result_artifacts=[
            _parametrized(artifact, params) for artifact in ir.result_artifacts
        ],
    )


def _parametrized(
    artifact: CollectedArtifact, params: dict[str, list[str]]
) -> CollectedArtifact:
    # The artifact's own parameters, plus the arguments every reference into
    # the path has to pass on. A reference's arguments are the *target's*
    # parameters: the slots reachable through it are a subset of the ones
    # reachable from here, so they are always in scope where the reference is
    # written.
    own = tuple(params.get(artifact.name, ()))
    if not own:
        return artifact
    match artifact:
        case CollectedModel():
            return dataclasses.replace(
                artifact,
                type_params=own,
                fields=[
                    dataclasses.replace(
                        model_field,
                        type_info=_pass_params(model_field.type_info, params),
                    )
                    for model_field in artifact.fields
                ],
            )
        case CollectedUnionAlias():
            return dataclasses.replace(
                artifact,
                type_params=own,
                variants=tuple(
                    dataclasses.replace(
                        variant, params=tuple(params.get(variant.name, ()))
                    )
                    for variant in artifact.variants
                ),
            )


def _pass_params(typ: TypeRef, params: dict[str, list[str]]) -> TypeRef:
    match typ:
        case NamedRef(name=name):
            return dataclasses.replace(typ, params=tuple(params.get(name, ())))
        case ListRef(element=element):
            return dataclasses.replace(typ, element=_pass_params(element, params))
        case ScalarRef():
            return typ


def _open_model_names(
    result_artifacts: list[CollectedArtifact],
    fragments: list[CollectedFragment],
) -> frozenset[str]:
    # A slot payload carries every passed fragment's fields next to the static
    # selection, and every fragment validates that same payload — so each
    # model inside a slot or fragment subtree tolerates the keys other readers
    # asked for (the open base) while its typed fields keep isolation intact.
    artifacts_by_name = {artifact.name: artifact for artifact in result_artifacts}
    roots = [
        *(model.name for model, _ in slot_roots(result_artifacts)),
        *(fragment.model_name for fragment in fragments),
    ]
    return frozenset(roots) | reachable_model_names(roots, artifacts_by_name)


def _collect_operation_vars(
    collector: PackageCollector, gql_vars: list[GQLVar]
) -> tuple[CollectedOperationVar, ...]:
    return tuple(
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
        for variable in gql_vars
    )


def _collect_on_type_bases(
    *,
    fragment_type_names: Iterable[str],
) -> list[CollectedOnTypeBase]:
    names = set(fragment_type_names)
    return [
        CollectedOnTypeBase(name=on_type_base_name(name), graphql_type_name=name)
        for name in sorted(names)
    ]


def _collect_fragments(
    collector: PackageCollector,
    statements: list[FragmentStatement],
    locations: dict[str, list[str]],
    spellings: dict[str, list[str]],
    *,
    schema: graphql.GraphQLSchema,
    slot_type_names: frozenset[str],
) -> tuple[list[CollectedFragment], list[GQLVar]]:
    # Полное пространство имён фрагментов пакета для обхода в
    # `fragment_closure`. Оно строится из тех же statements, что и копия в
    # `_expand_binds`, но раньше чтения bind: общий параметр только связал бы
    # два независимых и дешёвых lookup.
    fragment_defs = {statement.name: statement.definition for statement in statements}
    reader_class_names = {
        statement.name: (
            applied_fragment_class_name(capitalize_first(statement.name))
            if statement.kind == "factory"
            else capitalize_first(statement.name)
        )
        for statement in statements
    }
    fragments: list[CollectedFragment] = []
    # Собственные variables каждой factory возвращаются отдельно, чтобы
    # `collect_package_ir` включил их input-типы в closure артефактов. Это
    # требуется и для factory, несовместимой ни с одним slot: она не попадает
    # ни в одну combination, но её `with_args` всё равно должен ссылаться на
    # определённые в модуле типы.
    fragment_gql_vars: list[GQLVar] = []
    errors: list[str] = []
    for statement in statements:
        closure_names = fragment_closure(
            schema=schema,
            fragment=statement.definition,
            slot_types=slot_type_names,
            fragments=fragment_defs,
        )
        # Первым идёт собственный класс фрагмента, затем остальной closure по
        # алфавиту: base expression читается как «он сам плюс всё, что он
        # предлагает». `fragment_closure` всегда включает имя самого фрагмента.
        rest = sorted(
            reader_class_names[name] for name in closure_names if name != statement.name
        )
        class_name = capitalize_first(statement.name)
        match statement.kind:
            case "factory":
                # Параметры `with_args` принадлежат document closure самой
                # factory и не зависят от достигающих её combination.
                synthesized, var_errors = fragment_own_vars(
                    fragment=statement.definition,
                    dependencies=statement.dependencies,
                    schema=schema,
                    location=statement.stmt.location,
                )
                errors.extend(var_errors)
                arg_vars: list[
                    CollectedRequiredFragmentArg | CollectedOmittableFragmentArg
                ] = []
                for synthesized_var in synthesized:
                    gql_name = synthesized_var.node.variable.name.value
                    python_name = collector.to_snake_fn(gql_name)
                    fragment_gql_vars.append(
                        GQLVar(
                            name=gql_name,
                            gql_type=synthesized_var.explicit_value_type,
                        )
                    )
                    explicit_value_type = collector.collect_type(
                        synthesized_var.explicit_value_type
                    )
                    match synthesized_var:
                        case RequiredSynthesizedVar():
                            arg_vars.append(
                                CollectedRequiredFragmentArg(
                                    gql_name=gql_name,
                                    python_name=python_name,
                                    explicit_value_type=explicit_value_type,
                                )
                            )
                        case OmittableSynthesizedVar():
                            arg_vars.append(
                                CollectedOmittableFragmentArg(
                                    gql_name=gql_name,
                                    python_name=python_name,
                                    explicit_value_type=explicit_value_type,
                                )
                            )
                fragments.append(
                    CollectedFactoryFragment(
                        stmt_texts=tuple(spellings[statement.name]),
                        locations=tuple(locations[statement.name]),
                        class_name=class_name,
                        fragment_name=statement.name,
                        model_name=fragment_model_name(statement.name),
                        on_type=on_type_base_name(statement.type_condition),
                        closure=(reader_class_names[statement.name], *rest),
                        applied_class_name=applied_fragment_class_name(class_name),
                        arg_vars=tuple(arg_vars),
                    )
                )
            case "plain":
                fragments.append(
                    CollectedPlainFragment(
                        stmt_texts=tuple(spellings[statement.name]),
                        locations=tuple(locations[statement.name]),
                        class_name=class_name,
                        fragment_name=statement.name,
                        model_name=fragment_model_name(statement.name),
                        on_type=on_type_base_name(statement.type_condition),
                        closure=(reader_class_names[statement.name], *rest),
                    )
                )
    if errors:
        raise GraphQLGenerationError(errors)
    return fragments, fragment_gql_vars


def _collect_operations(
    collector: PackageCollector,
    queries: list[Operation],
    locations: dict[str, list[str]],
    spellings: dict[str, list[str]],
) -> list[CollectedOperation]:
    return [
        CollectedOperation(
            stmt_texts=tuple(spellings[query.name]),
            class_name=query.class_name,
            exec_source=query.exec_source,
            variables=_collect_operation_vars(collector, query.variables),
            is_subscription=query.is_subscription,
            locations=tuple(locations[query.name]),
        )
        for query in queries
    ]


def _slot_node_types(
    artifacts: list[CollectedArtifact],
) -> dict[str, tuple[str, ...]]:
    # One template's node models grouped by the slot each one belongs to, in
    # walk order. `slot_name` on the model is the canonical record of that
    # belonging -- this only re-keys it, over the artifacts of a single walk,
    # because a slot's name is unique per template and not per package.
    #
    # A response key identifies a slot for the whole operation -- one `bind()`
    # keyword, one node-type union -- while the generated node model is per
    # position, and one slot reaches several of them routinely: a polymorphic
    # parent alone gives the key one model per variant. Every position of the
    # key carries the same spliced fragments, so they are all readable through
    # those fragments' own `read(node)`.
    by_slot: dict[str, tuple[str, ...]] = {}
    for model, slot_name in slot_roots(artifacts):
        by_slot[slot_name] = (*by_slot.get(slot_name, ()), model.name)
    return by_slot


def _template_slot(
    collector: PackageCollector,
    slot: QuerySlot,
    node_types: dict[str, tuple[str, ...]],
    fragment_defs: dict[str, graphql.FragmentDefinitionNode],
) -> CollectedTemplateSlot:
    # The bases are derived from the very same compatibility predicate the
    # enumeration runs (`combinations.compatible_fragment_names`), so the
    # signature `bind()` renders and the combinations the dispatch table holds
    # cannot disagree about which fragments belong to this slot.
    bases = {
        on_type_base_name(fragment_defs[name].type_condition.name.value)
        for name in compatible_fragment_names(
            schema=collector.schema,
            slot_type=slot.type_name,
            fragments=fragment_defs,
        )
    }
    return CollectedTemplateSlot(
        name=slot.name,
        python_name=python_field_name(slot.name, collector.to_snake_fn),
        type_name=slot.type_name,
        node_types=node_types[slot.name],
        on_type_bases=tuple(sorted(bases)),
    )


def _statically_excluded_slot_errors(
    queries: list[Template], slot_node_types: dict[str, dict[str, tuple[str, ...]]]
) -> list[str]:
    # A slot's node type comes from the AST, but the slot model only exists
    # when the field survives collection -- a literally-always-false
    # @skip/@include drops it, leaving a template whose slot promises fragment
    # data that can never arrive. There is no earlier point that can say so:
    # an always-false condition on a *parent* prunes the walk before the slot
    # field is ever visited, so no node is dropped anywhere -- the absence of
    # any model carrying the slot's name, once the walk is over, is the fact
    # itself. That is also what lets `CollectedTemplateSlot.node_types` be
    # non-empty by construction. Gathered across every template first, so one
    # regeneration names every excluded slot in the package.
    return [
        _excluded_slot_error(slot.name, query)
        for query in queries
        for slot in query.slots
        if slot.name not in slot_node_types[query.name]
    ]


def _excluded_slot_error(slot_name: str, query: Template) -> str:
    return (
        f"Slot '{slot_name}' of template '{query.name}' at "
        f"{query.stmt.location} is statically excluded by its @skip/@include "
        f"directives"
    )


def _reject_slot_name_collisions(
    slots: tuple[CollectedTemplateSlot, ...], query: Template
) -> None:
    # A slot's `python_name` is the `bind()` keyword and the dispatch key, and
    # it comes off `to_snake_fn`, which is not injective -- so the one
    # namespace a template's slots share is checked here, where it is derived;
    # every later layer may key by it without re-checking.
    claims: dict[str, list[str]] = defaultdict(list)
    for slot in slots:
        claims[slot.python_name].append(slot.name)
    errors = [
        _slot_name_collision_error(derived, names, query)
        for derived, names in sorted(claims.items())
        if len(names) > 1
    ]
    if errors:
        raise GraphQLGenerationError(errors)


def _slot_name_collision_error(
    derived: str, response_keys: list[str], query: Template
) -> str:
    keys = ", ".join(repr(name) for name in response_keys)
    return (
        f"Slots {keys} of template '{query.name}' at {query.stmt.location} all "
        f"map to the Python name '{derived}'; alias one of the fields so their "
        "names differ"
    )


def _collect_templates(
    collector: PackageCollector,
    queries: list[Template],
    locations: dict[str, list[str]],
    spellings: dict[str, list[str]],
    template_artifacts: dict[str, list[CollectedArtifact]],
    *,
    fragment_defs: dict[str, graphql.FragmentDefinitionNode],
) -> list[CollectedTemplate]:
    slot_node_types = {
        name: _slot_node_types(artifacts)
        for name, artifacts in template_artifacts.items()
    }
    excluded_errors = _statically_excluded_slot_errors(queries, slot_node_types)
    if excluded_errors:
        raise GraphQLGenerationError(excluded_errors)
    templates: list[CollectedTemplate] = []
    for query in queries:
        node_types = slot_node_types[query.name]
        slots = tuple(
            _template_slot(collector, slot, node_types, fragment_defs)
            for slot in query.slots
        )
        _reject_slot_name_collisions(slots, query)
        templates.append(
            CollectedTemplate(
                stmt_texts=tuple(spellings[query.name]),
                name=query.name,
                class_name=query.class_name,
                variables=_collect_operation_vars(collector, query.variables),
                slots=slots,
                is_subscription=query.is_subscription,
                locations=tuple(locations[query.name]),
            )
        )
    return templates


def _binding_slot(
    template_slot: CollectedTemplateSlot,
    *,
    readable: tuple[ReadableFragment, ...],
    collected_fragment_by_name: dict[str, CollectedFragment],
) -> CollectedBindingSlot:
    return CollectedBindingSlot(
        slot=template_slot,
        readable_fragments=tuple(
            CollectedReadableFragment(
                fragment=collected_fragment_by_name[entry.name],
                typenames=tuple(sorted(entry.typenames)),
                direct=entry.direct,
            )
            for entry in readable
        ),
    )


def _collect_bindings(
    *,
    expanded_combinations: list[_ExpandedCombination],
    collected_fragment_by_name: dict[str, CollectedFragment],
) -> list[CollectedBinding]:
    bindings: list[CollectedBinding] = []
    for expanded_combination in expanded_combinations:
        expanded = expanded_combination.expanded
        template = expanded_combination.template
        slots = tuple(
            _binding_slot(
                template_slot,
                # Indexed, not defaulted: `readable_fragments` is total over
                # the template's slots, so a slot this bind left empty has an
                # empty entry and a missing key is the two sides having drifted
                # apart.
                readable=expanded.readable_fragments[template_slot.name],
                collected_fragment_by_name=collected_fragment_by_name,
            )
            for template_slot in template.slots
        )
        # Каноническая логическая идентичность комбинации. Runtime dispatch
        # отдельно использует generated definition classes, потому что строки
        # не доказывают происхождение fragment из сгенерированного API.
        collected_combination_key = combination_key(
            template.class_name,
            (
                (
                    binding_slot.slot.python_name,
                    tuple(
                        fragment.fragment_name
                        for fragment in binding_slot.direct_fragments
                    ),
                )
                for binding_slot in slots
            ),
        )
        bindings.append(
            CollectedBinding(
                combination_key=collected_combination_key,
                template=template,
                exec_source=expanded.exec_source,
                slots=slots,
                locations=expanded_combination.locations,
            )
        )
    return bindings
