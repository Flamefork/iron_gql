from typing import Protocol
from warnings import warn

import graphql

from iron_gql.codegen.accessors import field_type
from iron_gql.codegen.accessors import input_fields
from iron_gql.codegen.ir import CollectedArtifact
from iron_gql.codegen.ir import CollectedField
from iron_gql.codegen.ir import CollectedModel
from iron_gql.codegen.ir import CollectedUnionAlias
from iron_gql.codegen.ir import StrTransform
from iron_gql.codegen.ir import TypeRef
from iron_gql.codegen.parser import Query
from iron_gql.codegen.util import capitalize_first
from iron_gql.codegen.warnings import GraphQLDeprecationWarning


class TypeRefBuilder(Protocol):
    def __call__(
        self, gql_type: graphql.GraphQLType, *, nullable: bool = True
    ) -> TypeRef: ...


def collect_input_type_closure(
    queries: list[Query],
) -> list[graphql.GraphQLInputObjectType]:
    roots: set[graphql.GraphQLInputObjectType] = set()
    for query in queries:
        for variable in query.variables:
            named = graphql.get_named_type(variable.gql_type)
            if isinstance(named, graphql.GraphQLInputObjectType):
                roots.add(named)

    visited: set[str] = set()
    result: list[graphql.GraphQLInputObjectType] = []
    queue = sorted(roots, key=lambda typ: typ.name)
    while queue:
        typ = queue.pop()
        if typ.name in visited:
            continue
        visited.add(typ.name)
        result.append(typ)
        for gql_field in input_fields(typ).values():
            target = graphql.get_named_type(field_type(gql_field))
            if isinstance(target, graphql.GraphQLInputObjectType):
                queue.append(target)
    return sorted(result, key=lambda typ: typ.name)


def collect_input_artifacts(
    ordered_input_types: list[graphql.GraphQLInputObjectType],
    *,
    to_snake_fn: StrTransform,
    collect_type: TypeRefBuilder,
) -> list[CollectedArtifact]:
    artifacts: list[CollectedArtifact] = []
    for gql_type in ordered_input_types:
        if gql_type.is_one_of:
            artifacts.extend(
                _collect_one_of_input_type(
                    gql_type, to_snake_fn=to_snake_fn, collect_type=collect_type
                )
            )
        else:
            artifacts.append(
                _collect_regular_input_type(
                    gql_type, to_snake_fn=to_snake_fn, collect_type=collect_type
                )
            )
    return artifacts


def _collect_regular_input_type(
    gql_type: graphql.GraphQLInputObjectType,
    *,
    to_snake_fn: StrTransform,
    collect_type: TypeRefBuilder,
) -> CollectedModel:
    fields: list[CollectedField] = []
    for field_name, gql_field in input_fields(gql_type).items():
        if gql_field.deprecation_reason is not None:
            field_path = f"{gql_type.name}.{field_name}"
            reason = gql_field.deprecation_reason
            warn(
                f"Input field '{field_path}' is deprecated: {reason}",
                GraphQLDeprecationWarning,
                stacklevel=2,
            )
        fields.append(
            CollectedField(
                name=to_snake_fn(field_name),
                response_key=field_name,
                type_info=collect_type(field_type(gql_field)),
                default_expr=_input_default_expr(gql_field),
            )
        )
    return CollectedModel(name=gql_type.name, fields=fields)


def _collect_one_of_input_type(
    gql_type: graphql.GraphQLInputObjectType,
    *,
    to_snake_fn: StrTransform,
    collect_type: TypeRefBuilder,
) -> list[CollectedArtifact]:
    variant_names: list[str] = []
    artifacts: list[CollectedArtifact] = []
    for field_name, gql_field in input_fields(gql_type).items():
        if gql_field.deprecation_reason is not None:
            field_path = f"{gql_type.name}.{field_name}"
            reason = gql_field.deprecation_reason
            warn(
                f"Input field '{field_path}' is deprecated: {reason}",
                GraphQLDeprecationWarning,
                stacklevel=2,
            )
        variant_name = gql_type.name + capitalize_first(field_name)
        variant_names.append(variant_name)
        artifacts.append(
            CollectedModel(
                name=variant_name,
                fields=[
                    CollectedField(
                        name=to_snake_fn(field_name),
                        response_key=field_name,
                        type_info=collect_type(field_type(gql_field), nullable=False),
                    )
                ],
            )
        )
    artifacts.append(
        CollectedUnionAlias(name=gql_type.name, variants=tuple(variant_names))
    )
    return artifacts


def _input_default_expr(gql_field: graphql.GraphQLInputField) -> str | None:
    # graphql-core declares `default_value` as Any
    default_value: object = gql_field.default_value  # pyright: ignore[reportAny]
    if default_value != graphql.Undefined:
        return repr(default_value)
    if not isinstance(field_type(gql_field), graphql.GraphQLNonNull):
        return "None"
    return None
