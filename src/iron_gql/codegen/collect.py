from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Protocol
from typing import cast
from warnings import warn

import graphql
from graphql.execution.collect_fields import collect_fields
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
from iron_gql.codegen.parser import Query
from iron_gql.codegen.parser import Statement
from iron_gql.codegen.selection import build_codegen_variable_values
from iron_gql.codegen.selection import build_excluded_variable_values
from iron_gql.codegen.selection import interface_has_base_typename
from iron_gql.codegen.selection import merge_selection_sets
from iron_gql.codegen.selection import resolve_explicit_types
from iron_gql.codegen.util import capitalize_first
from iron_gql.codegen.warnings import GraphQLDeprecationWarning
from iron_gql.codegen.warnings import UnknownGQLTypeWarning
from iron_gql.codegen.warnings import warn_deprecated_field


@dataclass(kw_only=True, frozen=True)
class CollectionContext:
    query_name: str
    fragments: dict[str, graphql.FragmentDefinitionNode]
    variable_values: dict[str, Any]
    excluded_variable_values: dict[str, Any]


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

    def _collect_selected_fields(
        self,
        runtime_type: graphql.GraphQLObjectType,
        selection_set: graphql.SelectionSetNode,
        ctx: CollectionContext,
    ) -> tuple[
        dict[str, list[graphql.FieldNode]],
        dict[str, list[graphql.FieldNode]],
        set[str],
    ]:
        included_fields = collect_fields(
            self.schema,
            ctx.fragments,
            ctx.variable_values,
            runtime_type,
            selection_set,
        )
        excluded_fields = collect_fields(
            self.schema,
            ctx.fragments,
            ctx.excluded_variable_values,
            runtime_type,
            selection_set,
        )
        fields_by_key = {**excluded_fields, **included_fields}
        conditional_keys = fields_by_key.keys() - (
            included_fields.keys() & excluded_fields.keys()
        )
        return included_fields, fields_by_key, conditional_keys

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
            fragments=query.fragments,
            variable_values=build_codegen_variable_values(query.doc, query.variables),
            excluded_variable_values=build_excluded_variable_values(
                query.doc, query.variables
            ),
        )
        return self._collect_object_model(
            model_name_base=f"{capitalize_first(query.name)}Result",
            runtime_type=query.root_type,
            selection_set=query.operation_def.selection_set,
            ctx=ctx,
        )

    def _collect_object_model(
        self,
        *,
        model_name_base: str,
        runtime_type: graphql.GraphQLObjectType,
        selection_set: graphql.SelectionSetNode,
        ctx: CollectionContext,
        typename_type: TypeRef | None = None,
        require_typename_for: str | None = None,
        graphql_type_name: str | None = None,
    ) -> list[CollectedArtifact]:
        included_fields, fields_by_key, conditional_keys = (
            self._collect_selected_fields(
                runtime_type,
                selection_set,
                ctx,
            )
        )
        if require_typename_for is not None and "__typename" not in included_fields:
            msg = f"Missing __typename in selection set for '{require_typename_for}'"
            raise ValueError(msg)

        child_models: list[CollectedArtifact] = []
        fields: list[CollectedField] = []
        for response_key, field_nodes in fields_by_key.items():
            field_child_models, collected_field = self._collect_field(
                model_name_base=model_name_base,
                runtime_type=runtime_type,
                response_key=response_key,
                field_nodes=field_nodes,
                ctx=ctx,
                typename_type=typename_type,
                is_conditional=response_key in conditional_keys,
            )
            child_models.extend(field_child_models)
            fields.append(collected_field)

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
        field_nodes: list[graphql.FieldNode],
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

        representative = field_nodes[0]
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
            field_nodes=field_nodes,
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
        field_nodes: list[graphql.FieldNode],
        ctx: CollectionContext,
    ) -> tuple[list[CollectedArtifact], TypeRef]:
        selection_set = merge_selection_sets(field_nodes)
        if selection_set is None:
            return [], self.collect_type(gql_type)

        named = graphql.get_named_type(gql_type)
        child_base = model_name_base + capitalize_first(response_key)
        match named:
            case graphql.GraphQLObjectType():
                return (
                    self._collect_object_model(
                        model_name_base=child_base,
                        runtime_type=named,
                        selection_set=selection_set,
                        ctx=ctx,
                        graphql_type_name=named.name,
                    ),
                    self.collect_type(gql_type, child_model_name=child_base),
                )
            case graphql.GraphQLUnionType():
                possible = sorted(union_types(named), key=lambda typ: typ.name)
                return self._collect_polymorphic_models(
                    base_name=child_base,
                    possible_types=possible,
                    explicit_types=set(possible),
                    selection_set=selection_set,
                    ctx=ctx,
                    field_gql_type=gql_type,
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
                    selection_set=selection_set,
                    fragments=ctx.fragments,
                    interface_type=named,
                    possible_types=possible,
                )
                if explicit and not interface_has_base_typename(
                    selection_set, ctx.fragments, named.name
                ):
                    msg = (
                        f"Missing __typename in selection set for interface"
                        f" '{named.name}'"
                    )
                    raise ValueError(msg)
                return self._collect_polymorphic_models(
                    base_name=child_base,
                    possible_types=possible,
                    explicit_types=explicit,
                    selection_set=selection_set,
                    ctx=ctx,
                    field_gql_type=gql_type,
                    require_typename_for=named.name,
                )
            case _:
                msg = f"Unknown type {named} for field {response_key}"
                raise ValueError(msg)

    def _collect_polymorphic_models(
        self,
        *,
        base_name: str,
        possible_types: list[graphql.GraphQLObjectType],
        explicit_types: set[graphql.GraphQLObjectType],
        selection_set: graphql.SelectionSetNode,
        ctx: CollectionContext,
        field_gql_type: graphql.GraphQLType,
        require_typename_for: str,
    ) -> tuple[list[CollectedArtifact], TypeRef]:
        if not explicit_types:
            child_models = self._collect_object_model(
                model_name_base=base_name,
                runtime_type=possible_types[0],
                selection_set=selection_set,
                ctx=ctx,
                typename_type=ScalarRef(expr="str", name_hint="Str"),
                graphql_type_name=require_typename_for,
            )
            return (
                child_models,
                self.collect_type(field_gql_type, child_model_name=base_name),
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
                    selection_set=selection_set,
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
                    selection_set=selection_set,
                    ctx=ctx,
                    typename_type=fallback_typename,
                    require_typename_for=require_typename_for,
                    graphql_type_name=require_typename_for,
                )
            )
            union_types.append(fallback_name)

        return (
            [
                *child_models,
                CollectedUnionAlias(
                    name=base_name,
                    variants=tuple(union_types),
                    discriminator="typename__",
                ),
            ],
            self.collect_type(field_gql_type, child_model_name=base_name),
        )


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
