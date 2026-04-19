from warnings import warn

import graphql


class UnknownGQLTypeWarning(UserWarning):
    pass


class GraphQLDeprecationWarning(UserWarning):
    pass


def warn_deprecated_field(
    query_name: str,
    runtime_type: graphql.GraphQLObjectType,
    representative: graphql.FieldNode,
    field_def: graphql.GraphQLField,
) -> None:
    if field_def.deprecation_reason is not None:
        warn(
            f"Query '{query_name}': field"
            f" '{runtime_type.name}.{representative.name.value}'"
            f" is deprecated: {field_def.deprecation_reason}",
            GraphQLDeprecationWarning,
            stacklevel=2,
        )
    if representative.arguments:
        for arg_node in representative.arguments:
            schema_arg = field_def.args[arg_node.name.value]
            if schema_arg.deprecation_reason is not None:
                warn(
                    f"Query '{query_name}': argument"
                    f" '{arg_node.name.value}'"
                    f" on '{runtime_type.name}.{representative.name.value}'"
                    f" is deprecated:"
                    f" {schema_arg.deprecation_reason}",
                    GraphQLDeprecationWarning,
                    stacklevel=2,
                )
