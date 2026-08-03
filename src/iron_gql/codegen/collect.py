from collections import defaultdict
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
from iron_gql.codegen.collect_inputs import collect_input_artifacts
from iron_gql.codegen.collect_inputs import collect_input_type_closure
from iron_gql.codegen.ir import BUILTIN_SCALARS
from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedEnum
from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedOperation
from iron_gql.codegen.ir import CollectedOperationVar
from iron_gql.codegen.ir import CollectedPackageIR
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import GraphQLGenerationError
from iron_gql.codegen.ir import ImportRef
from iron_gql.codegen.ir import ListRef
from iron_gql.codegen.ir import NamedRef
from iron_gql.codegen.ir import ScalarRef
from iron_gql.codegen.ir import StrTransform
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.ir import make_optional
from iron_gql.codegen.names import validate_collected_names
from iron_gql.codegen.parser import Query
from iron_gql.codegen.parser import Statement
from iron_gql.codegen.selection import ALWAYS
from iron_gql.codegen.selection import ConditionalNode
from iron_gql.codegen.selection import SelectionRoots
from iron_gql.codegen.selection import collect_conditional_fields
from iron_gql.codegen.selection import interface_has_base_typename
from iron_gql.codegen.selection import resolve_explicit_types
from iron_gql.codegen.selection import uncovered_assignment
from iron_gql.codegen.util import capitalize_first
from iron_gql.codegen.warnings import GraphQLDeprecationWarning
from iron_gql.codegen.warnings import UnknownGQLTypeWarning
from iron_gql.codegen.warnings import warn_deprecated_field


@dataclass(kw_only=True, frozen=True)
class CollectionContext:
    query_name: str
    location: str
    fragments: dict[str, graphql.FragmentDefinitionNode]


def python_field_name(
    response_key: str,
    to_snake_fn: StrTransform,
) -> str:
    if response_key.startswith("__"):
        return to_snake_fn(f"{response_key[2:]}__")
    return to_snake_fn(response_key)


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
        return (
            self._collect_composite_model(
                base_name=child_base,
                named=graphql.get_named_type(gql_type),
                roots=roots,
                ctx=ctx,
                origin=f"field {response_key}",
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
    ) -> list[CollectedArtifact]:
        # The flat merge below feeds the existence-based walks (explicit
        # variants, base __typename), which do not read conditions.
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
    ) -> list[CollectedArtifact]:
        if not explicit_types:
            return self._collect_object_model(
                model_name_base=base_name,
                runtime_type=possible_types[0],
                roots=roots,
                ctx=ctx,
                typename_type=ScalarRef(expr="str", name_hint="Str"),
                graphql_type_name=require_typename_for,
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
    # Identical (dedented) text under one name is a harmless copy, differing
    # text is ambiguous. Every distinct literal spelling is kept per name —
    # the dispatch dict is keyed by the exact literal, so each spelling needs
    # its own entry.
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
    scalars: dict[str, ImportRef],
    to_snake_fn: StrTransform,
) -> CollectedPackageIR:
    queries, all_locations, query_spellings = _dedup_statements(queries, "queries")

    collector = PackageCollector(
        schema=schema,
        scalars=scalars,
        to_snake_fn=to_snake_fn,
    )
    result_artifacts: list[CollectedArtifact] = []
    for query in queries:
        result_artifacts.extend(collector.collect_operation_models(query))
    input_artifacts = collect_input_artifacts(
        collect_input_type_closure(queries),
        to_snake_fn=collector.to_snake_fn,
        collect_type=collector.collect_type,
    )
    operations = _collect_operations(collector, queries, all_locations, query_spellings)

    # Validated before any name-keyed structure is derived: the rename pass
    # resolves NamedRefs through these names.
    name_errors = validate_collected_names(
        result_artifacts=result_artifacts,
        input_artifacts=input_artifacts,
        enums=[collector.enums[name] for name in sorted(collector.enums)],
    )
    if name_errors:
        raise GraphQLGenerationError(name_errors)

    return CollectedPackageIR(
        result_artifacts=result_artifacts,
        input_artifacts=input_artifacts,
        operations=operations,
        enums=[collector.enums[name] for name in sorted(collector.enums)],
    )


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
        class_name = capitalize_first(query.name)
        operations.append(
            CollectedOperation(
                stmt_texts=tuple(spellings[query.name]),
                class_name=class_name,
                result_type=f"{class_name}Result",
                exec_source=query.exec_source,
                variables=variables,
                is_subscription=(
                    query.operation_def.operation == graphql.OperationType.SUBSCRIPTION
                ),
                locations=tuple(all_locations[query.name]),
            )
        )
    return operations
