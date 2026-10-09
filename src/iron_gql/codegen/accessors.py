from typing import Any

import graphql

# Typed accessors over graphql-core
#
# Stub holes in graphql-core 3.3, all localized here so the rest of the
# codebase stays fully typed. A function boundary with a declared return type
# is the only construct that stops the taint from spreading, hence the helper
# shape. Attribute accessors are named <owner>_<attribute>; `visit_document`
# wraps a function, not an attribute, and keeps the function's name.
#
# Unknown from wrapping types: graphql-core parameterizes wrapping types via a
# Self-bound TypeVar whose bound (`GraphQLNullableType`) itself transitively
# embeds an unparameterized `GraphQLList`. As a result, every match/isinstance
# narrow leaks Unknown into `.of_type` and `.type`, and the Unknown also
# propagates into the call-site argument type after `match`. `Any` on the
# `wrapping_of_type` parameter accepts the Unknown-tainted match output without
# polluting call-sites (any narrower parameter type re-triggers
# reportUnknownArgumentType there), and the assert keeps the runtime contract
# honest.
#
# Any from untyped `cached_property`: graphql-core re-exports cached_property
# through a conditional import that pyright resolves to Any, so `.fields`,
# `.types` and `.values` on schema types are Any. The field/type/value
# accessors pin them back to their documented types.
#
# Any from `graphql.visit`: its declared return type is a bare `Any` (it can
# return any node, depending on what a visitor does), even though every call
# site in this codebase knows the concrete type it put in and expects back.
# `visit_document` pins that contract for the call sites that use the
# return value; call sites that only rely on visitor side effects can keep
# calling `graphql.visit` directly and discard the untyped result.
#
# Unknown from `TypeInfo.get_type`: the stub leaves the return unannotated, so
# `type_info_type` pins it to the documented `GraphQLType | None`.
#
# Unknown from `TypeInfo.get_input_type`, on top of the stub hole above:
# `GraphQLInputType` is itself a `Union[..., GraphQLWrappingType]` with the
# same bare, unparameterized `GraphQLWrappingType` member described above, so
# even a fully-annotated wrapper would still return an Unknown-tainted type.
# `type_info_input_type` returns the untainted `GraphQLType` base instead — the
# same widening `wrapping_of_type` already does for the identical reason —
# rather than reproducing a tainted alias under a new name.
#
# Unknown from `coerce_input_literal`: its `type_` parameter is that same
# tainted alias, and its return is a bare `Any` (a coerced input value of any
# shape). `coerce_input_field_literal` takes the input field instead of its
# type, so the tainted alias never leaves this module.


def wrapping_of_type(gql_type: Any) -> graphql.GraphQLType:  # pyright: ignore[reportAny]
    assert isinstance(gql_type, graphql.GraphQLWrappingType), (  # noqa: S101
        "wrapping_of_type expects a GraphQLNonNull or GraphQLList"
    )
    return gql_type.of_type


def field_type(
    field: graphql.GraphQLField | graphql.GraphQLInputField,
) -> graphql.GraphQLType:
    return field.type  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def object_fields(
    gql_type: graphql.GraphQLObjectType,
) -> dict[str, graphql.GraphQLField]:
    return gql_type.fields  # pyright: ignore[reportAny]


def input_fields(
    gql_type: graphql.GraphQLInputObjectType,
) -> dict[str, graphql.GraphQLInputField]:
    return gql_type.fields  # pyright: ignore[reportAny]


def union_types(
    gql_type: graphql.GraphQLUnionType,
) -> tuple[graphql.GraphQLObjectType, ...]:
    return tuple(gql_type.types)  # pyright: ignore[reportAny]


def type_info_type(
    type_info: graphql.TypeInfo,
) -> graphql.GraphQLType | None:
    return type_info.get_type()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def type_info_input_type(
    type_info: graphql.TypeInfo,
) -> graphql.GraphQLType | None:
    return type_info.get_input_type()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def type_from_ast(
    schema: graphql.GraphQLSchema, node: graphql.TypeNode
) -> graphql.GraphQLType | None:
    return graphql.type_from_ast(  # pyright: ignore[reportUnknownMemberType]
        schema, node
    )


def visit_document(
    doc: graphql.DocumentNode, visitor: graphql.Visitor
) -> graphql.DocumentNode:
    return graphql.visit(doc, visitor)  # pyright: ignore[reportAny]


def enum_values(
    gql_type: graphql.GraphQLEnumType,
) -> dict[str, graphql.GraphQLEnumValue]:
    return gql_type.values  # pyright: ignore[reportAny]


def coerce_input_field_literal(
    field: graphql.GraphQLInputField, literal: graphql.ConstValueNode
) -> object:
    return graphql.coerce_input_literal(  # pyright: ignore[reportUnknownMemberType, reportAny]
        literal,
        field.type,  # pyright: ignore[reportUnknownMemberType]
    )
